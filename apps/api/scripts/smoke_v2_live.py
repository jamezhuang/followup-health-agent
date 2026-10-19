"""Opt-in real-provider acceptance using synthetic records and local test accounts."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import httpx


async def main(base):
    root=Path(__file__).resolve().parents[1]
    accounts=json.loads((root/'.cache/initial-accounts.json').read_text(encoding='utf-8'))
    account=next(x for x in accounts if x['role']=='admin')
    subject=None
    async with httpx.AsyncClient(base_url=base,timeout=30) as client:
        response=await client.post('/auth/login',json={'username':account['username'],'password':account['password']})
        response.raise_for_status()
        try:
            response=await client.post('/v2/subjects',json={'name':'Synthetic live acceptance','diseases':['diabetes'],
                'constraints':'This is synthetic acceptance data. Do not diagnose or change medication. Only summarize confirmed records.'})
            response.raise_for_status()
            subject=response.json()['id']
            for day,value in [('2026-09-01','6.0'),('2026-10-01','6.2')]:
                response=await client.post('/v2/records',json={'subject':subject,'date':day,'kind':'observation',
                    'label':'Fasting glucose','value':value,'unit':'mmol/L'})
                response.raise_for_status()
            response=await client.post('/v2/runs',json={'subject':subject,
                'goal':'请把两次已确认的空腹血糖记录按日期整理成复诊资料，指出缺少什么。没有医学证据时不要解释病因或建议用药。'})
            response.raise_for_status()
            identifier=response.json()['id']
            started=time.monotonic()
            while time.monotonic()-started<200:
                await asyncio.sleep(1)
                response=await client.get('/v2/runs/'+identifier)
                response.raise_for_status()
                row=response.json()
                if row['status']!='running':break
            else:
                await client.post('/v2/runs/'+identifier+'/cancel')
                raise RuntimeError('acceptance_timeout')
            trace=(await client.get('/v2/admin/traces/'+identifier)).json()
            result=row.get('result') or {}
            report={'status':row['status'],'error_code':row.get('error_code'),
                'mode':result.get('mode'),'records':len(result.get('records',[])),
                'claims':len(result.get('claims',[])),'iterations':result.get('iterations'),
                'stop_reason':result.get('stop_reason'),'duration_ms':row['duration_ms'],
                'calls':[{k:v for k,v in call.items() if k in {'agent','purpose','model','status',
                    'input_tokens','output_tokens','total_tokens','duration_ms','error_code'}} for call in trace['calls']],
                'all_quotes_present':all(c['quote'] in next(s['text'] for s in result['sources'] if s['id']==c['evidence_id']) for c in result.get('claims',[]))}
            (root/'.cache/live-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=True,indent=2))
            if row['status']!='completed' or result.get('mode')!='live' or len(result.get('records',[]))!=2 or not trace['calls']:
                raise RuntimeError('live_acceptance_failed')
        finally:
            if subject:
                await client.delete('/v2/subjects/'+subject)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:8002')
    asyncio.run(main(parser.parse_args().base))
