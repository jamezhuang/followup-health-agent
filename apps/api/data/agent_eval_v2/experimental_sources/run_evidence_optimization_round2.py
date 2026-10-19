"""Frozen 65-case ablation: same cached candidates, production evidence selector, full review."""
import argparse,asyncio,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.run_agent_experiments import Client,save,digest,GEN_PROMPT,JUDGE_PROMPT,Judgment,retrieval_metrics,token_summary
from scripts.evaluate_agent_suite import validate,rows,DATA
from app.config import Settings
from app.followup.store import Repository
from app.followup.observability import calling
from app.followup.hospital_planner import plan_hospital
from app.followup.evidence_selection import select_task_sources
from app.followup.rag_pipeline import verify_answer
from app.followup.retrieval import hybrid_search

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--expand',action='store_true');args=parser.parse_args()
    corpus=validate();cases=rows('retrieval_generation.jsonl');byid={d['id']:d for d in corpus}
    baseline=ROOT/'.cache/agent_experiments/acfabe8a336f'
    rankings=json.loads((baseline/'rankings.json').read_text(encoding='utf8'))['hybrid_rerank']
    meta={'baseline':str(baseline),'cases':digest(cases),'selector':digest((ROOT/'app/followup/evidence_selection.py').read_text(encoding='utf8')),'script':digest(Path(__file__).read_text(encoding='utf8')),'expand':args.expand,'model':Settings().chat_model}
    out=ROOT/'.cache/evidence_optimization'/digest(meta)[:12];save(out/'metadata.json',meta)
    repo=Repository(out/'calls.sqlite3');client=Client(Settings(),repo);slots=asyncio.Semaphore(4)
    async def run(case):
        async with slots:
            path=out/(case['id']+'.json')
            if path.exists():return json.loads(path.read_text(encoding='utf8'))
            plan=plan_hospital(case['query'])
            tasks=plan.tasks or [{'query':q} for q in plan.queries()]
            candidates=[byid[key] for key in rankings[case['id']]]
            if args.expand and len(plan.names)>1:
                tasks=[{'query':name+' '+ ' '.join(plan.topics)+' '+case['query'].translate(str.maketrans('', '', '？?'))} for name in plan.names]
                tasks=[{'query':name+' '+ ' '.join(topic for topic in ['地址' if '地址' in case['query'] else '', '交通' if '交通' in case['query'] else ''])} for name in plan.names]
                for task in tasks:
                    for source in hybrid_search(task['query'],[{**d,'search_text':d['title']+' '+d['text']} for d in corpus],top_k=5):
                        if source['id'] not in {s['id'] for s in candidates}:candidates.append(source)
            selected=select_task_sources(tasks,candidates,3) if plan.names else candidates[:3]
            sources=[{**s,'kind':'public','authority':'synthetic_fixture'} for s in selected]
            try:
                with calling('base-'+case['id'],'评测','optimized_generation'):
                    draft=(await client.complete([{'role':'system','content':GEN_PROMPT},{'role':'user','content':json.dumps({'question':case['query'],'sources':sources},ensure_ascii=False)}]))['content']
                corrected,review=await verify_answer(client,repo,'correct-'+case['id'],case['query'],sources,draft,instructions=GEN_PROMPT)
                with calling('judge-'+case['id'],'辅助裁判','optimized_judge'):
                    judged=await client.structured(Judgment,JUDGE_PROMPT,{'question':case['query'],'answer':corrected,'given_sources':sources,'expected_behavior':case['expected_behavior'],'rubric':case['reference_answer'],'expected_evidence':[byid[g[0]] for g in case['gold_groups']],'must_not':case['must_not']})
                result={'id':case['id'],'status':'ok','source_ids':[s['id'] for s in sources],'tasks':tasks,'draft':draft,'answer':corrected,'review':review,'judgment':judged.model_dump()}
            except Exception as error:result={'id':case['id'],'status':'error','error':type(error).__name__}
            save(path,result);print(case['id']+' '+result['status'],flush=True);return result
    results=await asyncio.gather(*(run(c) for c in cases));valid=[r for r in results if r['status']=='ok']
    selected_rankings={r['id']:r['source_ids']+[k for k in rankings[r['id']] if k not in r['source_ids']] for r in valid}
    facts=sum(r['judgment']['supported_facts']+r['judgment']['unsupported_facts'] for r in valid)
    report={'metadata':meta,'planned_n':65,'completed_n':len(valid),'retrieval':retrieval_metrics([c for c in cases if c['id'] in selected_rankings],selected_rankings),
        'completion_n':sum(r['judgment']['complete'] for r in valid),'unsupported_answer_n':sum(r['judgment']['unsupported_facts']>0 for r in valid),
        'faithfulness':sum(r['judgment']['supported_facts'] for r in valid)/facts if facts else None,
        'tokens':{name:token_summary(repo,prefix,65) for name,prefix in [('generation','base-'),('verification','correct-'),('judge','judge-')]},
        'rows':results,'artifacts':str(out),'limits':['same-model auxiliary judge','development fixtures, not clinical validation','reranking reused; no new embedding/reranking cost','compare candidate selection, not full production search']}
    save(DATA/('evidence_expanded_results.json' if args.expand else 'evidence_optimization_results.json'),report);print('COMPLETE '+str(out),flush=True)
if __name__=='__main__':asyncio.run(main())
