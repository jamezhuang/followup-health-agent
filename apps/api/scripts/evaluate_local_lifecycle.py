"""Fresh SQLite publication/retrieval regression; never reads the production corpus."""
import asyncio
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from statistics import mean
from unittest.mock import patch

import httpx
from pydantic import BaseModel, Field

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.storage import Store
from app.v2_main import create_app
from app.followup.store import Repository
from app.followup.observability import calling
from app.followup.retrieval import public_chunks, hybrid_search, cosine
from app.followup.public_rag import public_sources
from scripts.run_agent_experiments import Client,save,digest,token_summary,retrieval_metrics,percentile,GEN_PROMPT,JUDGE_PROMPT,Judgment


class Citation(BaseModel):
    source_id:str
    quote:str=Field(min_length=1)


class Answer(BaseModel):
    answer:str
    citations:list[Citation]


def validate_published(repo, report, corpus, dimensions):
    import math
    docs={doc['id']:doc for doc in repo.items('*','knowledge')}
    gold={doc['id']:doc for doc in corpus}
    if len(docs)!=len(gold):return False
    for row in report['publication']:
        doc=docs.get(row['document_id']);expected=gold[row['fixture_id']]
        if not doc or doc['status']!='published' or doc['title']!=expected['title'] or doc['url']!=expected['url']:
            return False
        if [piece['text'] for piece in doc['pieces']]!=[expected['text']]:return False
        if doc.get('retrieval_index',{}).get('revision')!=doc['revision']:return False
        for chunk in public_chunks(doc,doc.get('embedding_model')):
            vector=chunk.get('embedding')
            if not vector or len(vector)!=dimensions or not all(math.isfinite(v) for v in vector):return False
    return True


async def main():
    dataset=ROOT/'data/agent_eval_v2'
    corpus=json.loads((dataset/'corpus.json').read_text(encoding='utf8'))
    all_cases=[json.loads(line) for line in (dataset/'retrieval_generation.jsonl').read_text(encoding='utf8').splitlines()]
    excluded={f'D{i:02}' for i in range(25,33)}|{'D38'}
    corpus=[doc for doc in corpus if doc['id'] not in excluded]
    ids={doc['id'] for doc in corpus}
    cases=[case for case in all_cases if all(set(group)<=ids for group in case['gold_groups'])]
    out=ROOT/'data/local_lifecycle_eval'/uuid.uuid4().hex[:10]
    out.mkdir(parents=True)
    settings=Settings(mode='live',external_enabled=False)
    repo=Repository(out/'evaluation.sqlite3')
    client=Client(settings,repo)
    report={'status':'running','scope':'historical synthetic public subset; regression, not blind or clinical evaluation',
        'corpus_n':len(corpus),'cases_n':len(cases),'excluded_private_or_draft_ids':sorted(excluded),
        'corpus_sha256':digest(corpus),'cases_sha256':digest(cases),
        'implementation_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
          ['app/followup/retrieval.py','app/followup/public_rag.py','app/followup/knowledge_publish.py','app/v2_main.py']},
        'models':{'embedding':settings.embedding_model,'dimensions':settings.dimensions,'generation':settings.chat_model},
        'publication':[],'retrieval':{},'generation':[],'external_gate':[],
        'limits':['Same-model judgment is auxiliary, not independent verification.',
                  'Citation quote matching verifies provenance, not full semantic support.',
                  'No real external search is performed; gate tools are instrumented stubs.',
                  'No new holdout labels or clinical correctness are certified.']}
    save(out/'frozen_inputs.json',{'corpus':corpus,'cases':cases})
    save(out/'report.json',report)
    print('OUTPUT '+str(out),flush=True)
    if not settings.embedding_key:
        report.update(status='blocked',reason='embedding_not_configured')
        save(out/'report.json',report);return
    accounts=Store('sqlite:///'+(out/'auth.sqlite3').as_posix())
    accounts.register('localtestadmin','synthetic123456',role='admin')
    app=create_app(settings,repo,client,accounts)
    mapping={}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
            (await api.post('/auth/login',json={'username':'localtestadmin','password':'synthetic123456'})).raise_for_status()
            semaphore=asyncio.Semaphore(3)
            async def publish(doc):
                async with semaphore:
                    started=time.monotonic()
                    row=(await api.post('/v2/admin/knowledge',json={'title':doc['title'],
                        'pieces':[{'location':'合成资料正文','text':doc['text']}],
                        'url':doc['url'],'institution':'合成评测机构，非真实医院',
                        'published_at':'2026-10-19','rights':'合成回归资料，禁止发布到生产库'})).json()
                    response=await api.post('/v2/admin/knowledge/'+row['id']+'/publish-task',json={'revision':row['revision']})
                    response.raise_for_status();task=response.json()
                    while time.monotonic()-started<240:
                        result=(await api.get('/v2/admin/knowledge-publications')).json()['items']
                        current=next(item for item in result if item['id']==task['id'])
                        if current['status'] in {'failed','completed'}:break
                        await asyncio.sleep(.2)
                    success=current['status']=='completed'
                    if success:mapping[row['id']]=doc['id']
                    report['publication'].append({'fixture_id':doc['id'],'document_id':row['id'],
                        'status':current['status'],'error_code':current['error_code'],
                        'elapsed_ms':round((time.monotonic()-started)*1000,2)})
                    save(out/'report.json',report)
                    print('publish '+doc['id']+' '+current['status'],flush=True)
            await asyncio.gather(*(publish(doc) for doc in corpus))
            if len(mapping)!=len(corpus):
                report.update(status='incomplete',reason='publication_failed');save(out/'report.json',report);return
            candidates=[part for doc in repo.items('*','knowledge') if doc['status']=='published'
                        for part in public_chunks(doc,settings.embedding_model)]
            with calling('query-vectors','评测','shared_query_embedding'):
                query_vectors=[]
                for offset in range(0,len(cases),16):query_vectors.extend(await client.embed([c['query'] for c in cases[offset:offset+16]]))
            rankings={mode:{} for mode in ['bm25','dense','hybrid']};details={}
            for case,vector in zip(cases,query_vectors):
                details[case['id']]={}
                for mode in rankings:
                    started=time.perf_counter()
                    if mode=='dense':
                        ranked=sorted([{**row,'score':cosine(vector,row['embedding'])} for row in candidates],key=lambda row:row['score'],reverse=True)
                        ranked=[row for row in ranked if row['score']>0][:10]
                    else:ranked=hybrid_search(case['query'],candidates,vector if mode=='hybrid' else None,top_k=10)
                    elapsed=(time.perf_counter()-started)*1000
                    doc_ids=list(dict.fromkeys(mapping[row['document_id']] for row in ranked))
                    rankings[mode][case['id']]=doc_ids
                    details[case['id']][mode]={'fixture_ids':doc_ids,'source_ids':[r['id'] for r in ranked],
                                              'ranking_ms':elapsed,'sources':ranked[:4]}
            for mode,ranks in rankings.items():
                latencies=[details[c['id']][mode]['ranking_ms'] for c in cases]
                report['retrieval'][mode]={'metrics':retrieval_metrics(cases,ranks),
                    'by_split':{split:retrieval_metrics([c for c in cases if c['split']==split],ranks) for split in ['dev','test']},
                    'ranking_p50_ms':percentile(latencies,.5),'ranking_p95_ms':percentile(latencies,.95),
                    'latency_scope':'ranking only; query embedding shared and accounted separately',
                    'rankings':ranks}
            save(out/'report.json',report)
            # Small paired generation diagnostic, labels never enter generation inputs.
            selected=[c for c in cases if c['id'] in {'R001','R041','R042','R045'}]
            async def generate(case,mode):
                async with semaphore:
                    run_id='generation-'+mode+'-'+case['id'];sources=details[case['id']][mode]['sources']
                    packet=[{k:s[k] for k in ('id','title','text','url','location')} for s in sources]
                    started=time.monotonic()
                    try:
                        with calling(run_id,'评测生成','answer'):
                            answer=await client.structured(Answer,GEN_PROMPT+' 每个引用给出source_id和逐字原文quote；正文中用[source_id]标记引用。',
                                {'question':case['query'],'sources':packet})
                        byid={s['id']:s for s in sources}
                        citation_checks=[{'source_id':c.source_id,'valid':c.source_id in byid and c.quote in byid[c.source_id]['text'],
                                          'linked_in_answer':c.source_id in answer.answer} for c in answer.citations]
                        gold=set().union(*(set(group) for group in case['gold_groups']))
                        with calling(run_id,'评测裁判','judge'):
                            judgment=await client.structured(Judgment,JUDGE_PROMPT,{'question':case['query'],
                                'answer':answer.answer,'given_sources':packet,
                                'expected_evidence':[doc for doc in corpus if doc['id'] in gold]})
                        item={'id':case['id'],'mode':mode,'status':'ok','answer':answer.model_dump(),
                              'citation_checks':citation_checks,'judgment':judgment.model_dump()}
                    except Exception as error:item={'id':case['id'],'mode':mode,'status':'failed','error_type':type(error).__name__}
                    item['elapsed_ms']=(time.monotonic()-started)*1000
                    report['generation'].append(item);save(out/'report.json',report)
                    print(run_id+' '+item['status'],flush=True)
            await asyncio.gather(*(generate(case,mode) for case in selected for mode in rankings))
            # Exercise the actual public_sources gate, while preventing live web access.
            from types import SimpleNamespace
            gate_events=[]
            class ProbeTools:
                def __init__(self,*args):self.providers=SimpleNamespace(available=lambda:['instrumented_stub'])
                async def search(self,args):gate_events.append(args['query']);return {'ok':True,'candidates':[]}
            gate_settings=Settings(mode='live',external_enabled=True)
            gate_cases=selected+[c for c in cases if not c['gold_groups']][:2]
            with patch('app.web_tools.WebTools',ProbeTools):
                for case in gate_cases:
                    before=len(gate_events);started=time.monotonic()
                    result=await public_sources(gate_settings,repo,client,'gate-'+case['id'],case['query'],[])
                    retrieved={mapping[s['document_id']] for s in result['sources'] if s.get('document_id') in mapping}
                    coverage=bool(case['gold_groups']) and all(set(group)&retrieved for group in case['gold_groups'])
                    sufficient=result.get('evidence_review',{}).get('sufficient')
                    report['external_gate'].append({'id':case['id'],'expected_answerable':bool(case['gold_groups']),
                        'all_gold_groups_retrieved':coverage,'review_sufficient':sufficient,
                        'external_search_attempts':len(gate_events)-before,'status':result['external_status'],
                        'retrieved_fixture_ids':sorted(retrieved),'elapsed_ms':(time.monotonic()-started)*1000})
                    save(out/'report.json',report);print('gate '+case['id']+' '+str(sufficient),flush=True)
    accounts.engine.dispose()
    report['tokens']={prefix:token_summary(repo,prefix,n) for prefix,n in
        [('knowledge-',len(corpus)),('query-vectors',len(cases)),('generation-bm25',len(selected)),
         ('generation-dense',len(selected)),('generation-hybrid',len(selected)),('gate-',len(gate_cases))]}
    report.update(status='complete',input_objects_unchanged=digest(corpus)==report['corpus_sha256'],
                  published_content_matches_frozen=validate_published(repo,report,corpus,settings.dimensions))
    save(out/'report.json',report)
    print(json.dumps({'status':report['status'],'retrieval':{m:v['metrics'] for m,v in report['retrieval'].items()},'output':str(out)},ensure_ascii=False),flush=True)


if __name__=='__main__':asyncio.run(main())
