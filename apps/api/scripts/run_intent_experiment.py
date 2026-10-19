"""Compare fallback rules, raw classifier, and actual routing on the same 100 cases."""
import argparse
import ast
import uuid
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.followup import intent as module
from app.followup.intent import IntentDecision, analyze_intent, fallback_intent
from app.followup.task_plan import COORDINATOR_INSTRUCTIONS
from app.followup.store import Repository
from app.followup.observability import calling
from scripts.run_agent_experiments import Client, save, digest, percentile
from scripts.evaluate_agent_suite import validate, rows
from scripts.summarize_agent_experiments import pipeline_usage


def metrics(results,arm):
    valid=[row for row in results if row.get(arm) is not None]
    def label(obj):return obj['intent']+'/'+obj['consultation_type']
    classes=sorted({label(row['expected']) for row in results})
    confusion=Counter((label(row['expected']),label(row[arm])) for row in valid)
    f1=[]
    for cls in classes:
        tp=confusion[cls,cls];fp=sum(n for (gold,pred),n in confusion.items() if pred==cls and gold!=cls)
        fn=sum(n for (gold,pred),n in confusion.items() if gold==cls and pred!=cls)
        f1.append(2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0)
    return {'planned_n':len(results),'valid_n':len(valid),'missing_n':len(results)-len(valid),
        'main_intent_accuracy':sum(row[arm]['intent']==row['expected']['intent'] for row in valid)/len(results),
        'route_accuracy':sum(label(row[arm])==label(row['expected']) for row in valid)/len(results),
        'strict_accuracy':sum(all(row[arm][key]==row['expected'][key] for key in ['intent','consultation_type','operation']) for row in valid)/len(results),
        'route_macro_f1_on_valid':sum(f1)/len(f1),'confusion':{a+' -> '+b:n for (a,b),n in confusion.items()},
        'failure_ids':[row['id'] for row in results if not row.get(arm) or any(row[arm][key]!=row['expected'][key] for key in ['intent','consultation_type','operation'])]}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--fresh',action='store_true');parser.add_argument('--report-path',type=Path);args=parser.parse_args()
    validate();cases=rows('intent.jsonl');settings=Settings()
    adjudication_path=ROOT/'data/agent_eval_v2/label_adjudications.json'
    adjudications=json.loads(adjudication_path.read_text(encoding='utf-8')) if adjudication_path.exists() else {'labels':{}}
    for case in cases:
        if case['id'] in adjudications['labels']:
            case['expected']=adjudications['labels'][case['id']]['expected']
    src=Path(module.__file__).read_text(encoding='utf-8')
    tree=ast.parse(src)
    prompt=next(node.value for node in ast.walk(tree) if isinstance(node,ast.Constant) and isinstance(node.value,str) and node.value.startswith('你是主协调Agent，'))+COORDINATOR_INSTRUCTIONS
    meta={'dataset':digest(cases),'source':digest(src),'model':settings.chat_model,'temperature':0,'workers':4}
    if args.fresh:meta['execution_id']=uuid.uuid4().hex
    out=ROOT/'.cache/agent_intent_experiment'/digest(meta)[:12];out.mkdir(parents=True,exist_ok=True)
    save(out/'metadata.json',meta);repo=Repository(out/'calls.sqlite3');slots=asyncio.Semaphore(4)
    class Capturing(Client):
        raw=None
        async def structured(self,schema,prompt,data):
            result=await super().structured(schema,prompt,data)
            if schema is IntentDecision:self.raw=result.model_dump()
            return result
    async def run(case):
        async with slots:
            path=out/'cases'/(case['id']+'.json')
            if path.exists():return json.loads(path.read_text(encoding='utf-8'))
            client=Capturing(settings,repo);start=time.monotonic()
            effective=await analyze_intent(repo,client,True,'intent-'+case['id'],case['input'],case['history'],False)
            raw=client.raw
            if raw is None:
                try:
                    with calling('pure-extra-'+case['id'],'评测','intent_pure_model'):
                        raw=(await client.structured(IntentDecision,prompt,{'message':case['input'],'recent_dialogue':[],'has_selected_subject':False})).model_dump()
                except Exception:raw=None
            result={'id':case['id'],'input':case['input'],'expected':case['expected'],
                'rules':fallback_intent(case['input'],case['history']).model_dump(),
                'pure_model':raw,'current':effective.model_dump(),'elapsed_s':time.monotonic()-start}
            save(path,result);print(case['id'],flush=True);return result
    results=await asyncio.gather(*(run(case) for case in cases))
    # Pure model generally reuses actual classifier output; direct calls only fill local fast paths.
    with repo.db() as db:records=[{**dict(row),**json.loads(row['body'])} for row in db.execute('SELECT * FROM calls')]
    usage=pipeline_usage(records,('intent-',),100)
    pure_usage=pipeline_usage(records,('intent-','pure-extra-'),100)
    report={'metadata':meta,'metrics':{arm:metrics(results,arm) for arm in ['rules','pure_model','current']},
        'tokens':{'actual_routing':usage,'pure_model_reconstructed':pure_usage},
        'latency_pipeline_plus_extra_pure_calls':{'p50_s':percentile([row['elapsed_s'] for row in results],.5),'p95_s':percentile([row['elapsed_s'] for row in results],.95)},
        'label_adjudications':adjudications,
        'limits':['Only explicitly recorded cases human-adjudicated; other labels remain draft','pure model reuses raw classifier output, local fast paths filled separately',
                  'Token count includes actual retries; local fast-path no-call cases consume zero model tokens'],
        'rows':results,'artifacts':str(out)}
    save(args.report_path or ROOT/'data/agent_eval_v2/intent_results.json',report);print('COMPLETE '+json.dumps(report['metrics'],ensure_ascii=False),flush=True)


if __name__=='__main__':asyncio.run(main())
