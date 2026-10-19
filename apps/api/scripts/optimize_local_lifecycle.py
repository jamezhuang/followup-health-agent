"""Development-selected weighted fusion, provisional paraphrases and paired citation trial."""
import asyncio,json,sys,time,uuid
from pathlib import Path
from statistics import mean
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.followup.store import Repository
from app.followup.observability import calling
from app.followup.retrieval import public_chunks,hybrid_search
from app.followup.citation_checks import alias_sources,check_citations
from scripts.evaluate_local_lifecycle import Answer
from scripts.run_agent_experiments import Client,save,digest,token_summary,retrieval_metrics,GEN_PROMPT

PARAPHRASES=[
 ('P01','清禾那家医院有没有自己的官方网站？',['D01']),
 ('P02','清禾已经投入使用的院址一共有几处？筹备中的不要算。',['D02']),
 ('P03','我要到清禾北院看病，导航目的地填哪儿？',['D03']),
 ('P04','去清禾南院，该输入什么街道门牌？',['D04']),
 ('P05','坐轨道交通去清禾北院，下车站和出站口怎么选？',['D05']),
 ('P06','清禾南院旁边能坐哪几路公共汽车？',['D06']),
 ('P07','替家人到清禾调取病历副本，要带齐哪些证明？',['D12']),
 ('P08','在清禾住院前，手续在哪里办、证件怎么准备？',['D14']),
 ('P09','开车进清禾南院的停车场，应该走哪道入口？',['D16']),
 ('P10','清禾北院的门牌和最近地铁出口，一起告诉我。',['D03','D05']),
 ('P11','请同时给清禾南院的具体位置与附近公交线路。',['D04','D06']),
 ('P12','清禾探病时间前后两个通知不一致，应该照哪份来？',['D15','D33'])]


async def main():
    previous=ROOT/'data/local_lifecycle_eval/27768cd912'
    old=json.loads((previous/'report.json').read_text(encoding='utf8'))
    inputs=json.loads((previous/'frozen_inputs.json').read_text(encoding='utf8'))
    out=ROOT/'data/local_lifecycle_eval'/('optimization-'+uuid.uuid4().hex[:10]);out.mkdir(parents=True)
    repo=Repository(out/'calls.sqlite3');settings=Settings(mode='live',external_enabled=False);client=Client(settings,repo)
    snapshot=Repository(previous/'evaluation.sqlite3')
    docs=snapshot.items('*','knowledge')
    mapping={row['document_id']:row['fixture_id'] for row in old['publication']}
    candidates=[c for doc in docs for c in public_chunks(doc,settings.embedding_model)]
    assert len(docs)==30 and all(c.get('embedding') for c in candidates)
    paraphrases=[{'id':i,'query':q,'gold_groups':[[d] for d in gold],'split':'provisional_paraphrase'} for i,q,gold in PARAPHRASES]
    cases=inputs['cases'];all_cases=cases+paraphrases
    report={'status':'running','previous_run':previous.name,'runtime_default_changed':False,
        'paraphrase_labels':'agent-authored, not independently human-reviewed; not a certified blind test',
        'selection_rule':'maximize dev all_groups_at_3, then p_at_1, then prefer smaller lexical weight',
        'input_sha256':digest({'cases':all_cases,'corpus':inputs['corpus']}),
        'development':{},'comparison':{},'citations':[]}
    save(out/'frozen_inputs.json',{'cases':all_cases,'paraphrases':paraphrases})
    save(out/'report.json',report);print('OUTPUT '+str(out),flush=True)
    vectors=[]
    with calling('query-vectors','排序实验','embedding'):
        for offset in range(0,len(all_cases),16):vectors.extend(await client.embed([c['query'] for c in all_cases[offset:offset+16]]))
    save(out/'query_vectors.json',{'model':settings.embedding_model,'cases_sha256':digest(all_cases),'vectors':vectors})
    def ranking(weight):
        return {case['id']:list(dict.fromkeys(mapping[s['document_id']] for s in hybrid_search(case['query'],candidates,vector,top_k=10,lexical_weight=weight)))
                for case,vector in zip(all_cases,vectors)}
    development=[c for c in cases if c['split']=='dev' and c['gold_groups']]
    ranks={weight:ranking(weight) for weight in [1.,1.5,2.,4.]}
    for weight,rank in ranks.items():report['development'][str(weight)]=retrieval_metrics(development,rank)
    chosen=max(ranks,key=lambda w:(report['development'][str(w)]['all_groups_at_3'],report['development'][str(w)]['p_at_1'],-w))
    report['chosen_lexical_weight']=chosen
    save(out/'selection.json',{'chosen':chosen,'dev':report['development'],'rule':report['selection_rule']})
    # Selection is now frozen before test and paraphrase metrics are examined.
    bm25={c['id']:list(dict.fromkeys(mapping[s['document_id']] for s in hybrid_search(c['query'],candidates,top_k=10))) for c in all_cases}
    for label,rank in [('bm25',bm25),('equal_rrf',ranks[1.]),('selected_weight',ranks[chosen])]:
        report['comparison'][label]={name:retrieval_metrics(subset,rank) for name,subset in
            [('historical_test',[c for c in cases if c['split']=='test']),('paraphrases',paraphrases)]}
    save(out/'rankings.json',{'bm25':bm25,'equal_rrf':ranks[1.],'selected_weight':ranks[chosen]})
    save(out/'report.json',report)
    # Isolate citation format change: replay the exact original candidate lists, not improved rankings.
    by_fixture={mapping[c['document_id']]:c for c in candidates}
    by_case={c['id']:c for c in cases}
    sem=asyncio.Semaphore(3)
    async def trial(previous_answer):
        async with sem:
            code,mode=previous_answer['id'],previous_answer['mode']
            sources=[by_fixture[d] for d in old['retrieval'][mode]['rankings'][code][:4]]
            original_check=check_citations(previous_answer['answer']['answer'],previous_answer['answer']['citations'],sources)
            packet,aliases=alias_sources(sources)
            run_id='citation-'+mode+'-'+code;started=time.monotonic()
            try:
                with calling(run_id,'引用实验','alias_answer'):
                    answer=await client.structured(Answer,GEN_PROMPT+' 正文必须以[S1]格式标注引用；citations给出完整source_id和逐字原文quote。只使用所给短编号。',
                        {'question':by_case[code]['query'],'sources':packet})
                check=check_citations(answer.answer,[c.model_dump() for c in answer.citations],packet)
                item={'id':code,'mode':mode,'status':'ok','before':original_check,'after':check,
                      'answer':answer.model_dump(),'aliases':aliases}
            except Exception as error:item={'id':code,'mode':mode,'status':'failed','error_type':type(error).__name__,'before':original_check}
            item['elapsed_ms']=(time.monotonic()-started)*1000
            report['citations'].append(item);save(out/'report.json',report)
            print(run_id+' '+str(item.get('after',{}).get('passed')),flush=True)
    await asyncio.gather(*(trial(row) for row in old['generation']))
    report['tokens']={prefix:token_summary(repo,prefix,n) for prefix,n in [('query-vectors',len(all_cases)),('citation-',len(old['generation']))]}
    report['status']='complete';save(out/'report.json',report)
    print(json.dumps({'chosen_weight':chosen,'comparison':report['comparison'],
        'citation_before_pass':sum(r['before']['passed'] for r in report['citations']),
        'citation_after_pass':sum(r.get('after',{}).get('passed',False) for r in report['citations'])},ensure_ascii=False),flush=True)


if __name__=='__main__':asyncio.run(main())
