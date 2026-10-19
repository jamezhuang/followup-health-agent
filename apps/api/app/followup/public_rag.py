"""Public education: split/rewrite -> published KB -> missing-info web fallback."""
import asyncio
import re

from .rag_pipeline import active, rewrite_knowledge, review_evidence
from .retrieval import public_chunks, hybrid_search
from .hospital_planner import SENSITIVE
from .checkpoints import checkpoint_call


async def public_sources(settings,repo,client,run_id,goal,messages):
    live=getattr(settings,'mode','demo')=='live'
    planner=client if live else None
    queries=await checkpoint_call('knowledge_query_plan',{'goal':goal,'messages':messages},
        lambda:rewrite_knowledge(planner,repo,run_id,goal,messages))
    candidates=[source for doc in repo.items('*','knowledge')
                if doc.get('status')=='published' and doc.get('kind')!='private'
                for source in public_chunks(doc,getattr(settings,'embedding_model',''))]
    vectors = [None] * len(queries)
    if live and settings.embedding_key and any(source.get('embedding') for source in candidates):
        from .observability import calling
        try:
            with calling(run_id,'科普 Agent','query_embedding'):
                vectors = await client.embed(queries)
            if len(vectors) != len(queries):
                raise ValueError('embedding_count_mismatch')
        except Exception:
            vectors = [None] * len(queries)
            repo.stage(run_id,'科普 Agent','查询向量不可用，使用关键词检索',reason='query_embedding_unavailable')
    rankings=[hybrid_search(query,candidates,vector=vector,top_k=4,
              lexical_weight=getattr(settings,'public_rag_lexical_weight',1.0)) for query,vector in zip(queries,vectors)]
    sources=[];seen=set()
    for rank in range(4):
        for ranking in rankings:
            if rank<len(ranking) and ranking[rank]['id'] not in seen:
                source=ranking[rank];seen.add(source['id']);sources.append(source)
    sources=sources[:6 if len(queries)>1 else 4]
    repo.stage(run_id,'科普 Agent','检索公共知识',source_count=len(sources))
    review=await review_evidence(planner,repo,run_id,{'question':goal},sources)
    def retain_relevant(review):
        if review is None:
            return
        accepted=set(review.source_ids)
        rejected=len(sources)-sum(source['id'] in accepted for source in sources)
        sources[:]=[source for source in sources if source['id'] in accepted]
        repo.stage(run_id,'科普 Agent','按当前问题筛选证据',source_count=len(sources),
                   rejected_sources=rejected,reason='public_relevance_filter')
    retain_relevant(review)
    outcome={'sources':sources,'queries':queries,'external_status':'not_needed' if review and review.sufficient else 'not_requested'}
    if review:outcome['evidence_review']=review.model_dump()
    declined=bool(re.search(r'(?:不要|不用|无需|不需要|禁止).{0,8}(?:联网|上网|外部搜索)',goal))
    needs_web=not sources or review is None or not review.sufficient
    if not needs_web or not live or not settings.external_enabled or not active(planner) or declined or SENSITIVE.search(goal):
        return outcome
    from ..external import ExternalSearch
    from ..web_tools import WebTools
    try:
        tools=WebTools(ExternalSearch(settings),{},[])
    except Exception:
        outcome['external_status']='service_unavailable';return outcome
    if not tools.providers.available():
        outcome['external_status']='unconfigured';return outcome
    outcome['external_status']='no_results'
    initial_source_ids={source['id'] for source in sources}
    async def lookup():
        pages=0;visited=set()
        for query in queries[:2]:
            if SENSITIVE.search(query):continue
            found=await tools.search({'query':query,'limit':5,'scope':'public'})
            repo.stage(run_id,'科普 Agent','补查公共资料',status='ok' if found.get('ok') else 'failed',
                provider_attempts=found.get('provider_attempts',[]),reason=found.get('outcome'))
            if not found.get('ok'):
                if outcome['external_status']!='partial':outcome['external_status']='service_unavailable'
                continue
            selected=[]
            from .source_policy import source_priority
            for item in sorted(found.get('candidates',[]),key=source_priority):
                key=item.get('url') or item['candidate_id']
                if key not in visited:
                    visited.add(key);selected.append(item)
            selected=selected[:max(0,3-pages)];pages+=len(selected)
            if not selected:continue
            if outcome['external_status']!='partial':outcome['external_status']='unreadable'
            results=await asyncio.gather(*(tools.read({'candidate_id':item['candidate_id'],
                'query':query,'method':'auto'}) for item in selected),return_exceptions=True)
            for result in results:
                if not isinstance(result,dict) or not result.get('ok'):continue
                for source in result.get('sources',[]):
                    if source['id'] not in seen:
                        seen.add(source['id']);sources.append({**source,'kind':'public',
                            'authority':result.get('authority','unverified')})
                        outcome['external_status']='partial'
            if pages>=3:break
    try:
        await asyncio.wait_for(lookup(),30)
    except asyncio.CancelledError:raise
    except Exception:
        outcome['external_status']='partial' if sources else 'service_unavailable'
    # Reassess new pages against the actual question; read success is not relevance.
    if any(source['id'] not in initial_source_ids for source in sources):
        final_review=await review_evidence(planner,repo,run_id,{'question':goal},sources)
        if final_review:
            retain_relevant(final_review)
            outcome['evidence_review']=final_review.model_dump()
            if final_review.sufficient:outcome['external_status']='ok'
    # A later network failure never discards relevant earlier evidence.
    outcome['sources']=sources[:9]
    return outcome
