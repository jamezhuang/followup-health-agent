"""Live acceptance of published corpus through the real follow-up workflow."""
import asyncio
import json
import time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]


async def main():
    accounts=json.loads((ROOT/'.cache/initial-accounts.json').read_text(encoding='utf-8'))
    account=next(x for x in accounts if x['role']=='admin')
    subject=None
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8002',timeout=30) as client:
        r=await client.post('/auth/login',json={k:account[k] for k in ('username','password')});r.raise_for_status()
        try:
            r=await client.post('/v2/subjects',json={'name':'知识库验收（虚构对象）','diseases':['hypertension'],
                'constraints':'仅验证资料检索与引用，不作诊断，不调整药物。'});r.raise_for_status();subject=r.json()['id']
            for day,value in [('2026-09-01','135'),('2026-10-01','136')]:
                r=await client.post('/v2/records',json={'subject':subject,'date':day,'kind':'observation',
                    'label':'收缩压','value':value,'unit':'mmHg'});r.raise_for_status()
            r=await client.post('/v2/runs',json={'subject':subject,'goal':
                '准备高血压复诊：整理已确认的两次血压记录，并根据本地《成人高血压食养指南（2023年版）》中“监测血压，自我管理”段落，补充一条自我管理要点及逐字出处。不要诊断，不调整药物。'})
            r.raise_for_status();identifier=r.json()['id'];started=time.monotonic()
            while time.monotonic()-started<210:
                await asyncio.sleep(1)
                r=await client.get('/v2/runs/'+identifier);r.raise_for_status();row=r.json()
                if row['status']!='running':break
            else:
                await client.post('/v2/runs/'+identifier+'/cancel')
                raise RuntimeError('Acceptance timed out')
            r=await client.get('/v2/admin/traces/'+identifier);r.raise_for_status();trace=r.json()
            result=row.get('result') or {};sources={s['id']:s for s in result.get('sources',[])}
            claims=result.get('claims',[])
            public=[c for c in claims if sources.get(c['evidence_id'],{}).get('kind')=='public']
            report=dict(run_id=identifier,status=row['status'],iterations=result.get('iterations'),
                stop_reason=result.get('stop_reason'),claims=len(claims),public_claims=len(public),
                all_quotes_present=bool(claims) and all(c['quote'] in sources.get(c['evidence_id'],{}).get('text','') for c in claims),
                public_citations=[dict(title=sources[c['evidence_id']]['title'],location=sources[c['evidence_id']]['location'],
                    url=sources[c['evidence_id']]['url']) for c in public],
                calls=[{k:v for k,v in c.items() if k in {'agent','purpose','status','input_tokens','output_tokens','total_tokens','duration_ms','error_code'}} for c in trace['calls']])
            report['passed']=row['status']=='completed' and bool(public) and report['all_quotes_present']
            (ROOT/'data/official_batch_01/live_acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False,indent=2))
            if not report['passed']:raise RuntimeError('Inspect live_acceptance.json')
        finally:
            if subject:
                r=await client.delete('/v2/subjects/'+subject);r.raise_for_status()


if __name__=='__main__':asyncio.run(main())
