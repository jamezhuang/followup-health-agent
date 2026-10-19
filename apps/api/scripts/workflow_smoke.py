"""Server-side smoke client. Never retries POST automatically."""
import argparse,json,os,time
import httpx

def main():
    parser=argparse.ArgumentParser();parser.add_argument('question');parser.add_argument('--base-url',default='http://127.0.0.1:8002');parser.add_argument('--wait-seconds',type=int,default=120)
    args=parser.parse_args();key=os.environ.get('WORKFLOW_API_KEY','')
    if not key:parser.error('Set WORKFLOW_API_KEY in the server-side environment first')
    if not 1<=args.wait_seconds<=300:parser.error('wait-seconds must be 1..300')
    with httpx.Client(base_url=args.base_url,headers={'X-CareBrief-Key':key},timeout=15) as client:
        response=client.post('/v2/integrations/workflow/tasks',json={'question':args.question});response.raise_for_status()
        row=response.json();identifier=row['id'];print('task_id='+identifier,flush=True)
        deadline=time.monotonic()+args.wait_seconds
        while row['status']=='running' and time.monotonic()<deadline:
            time.sleep(1);response=client.get('/v2/integrations/workflow/tasks/'+identifier);response.raise_for_status();row=response.json()
        print(json.dumps(row,ensure_ascii=False,indent=2))
        if row['status']=='running':print('Local wait ended; task continues. Poll the same task ID.');return 2
        return 0 if row['status']=='completed' else 1

if __name__=='__main__':raise SystemExit(main())
