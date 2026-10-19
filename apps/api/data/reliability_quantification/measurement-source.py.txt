"""Isolated outcome-based measurements. Live model only for checkpoint cost comparison."""
import argparse,asyncio,json,sys,time
from pathlib import Path
from datetime import datetime,timezone
from contextlib import nullcontext
import httpx

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.storage import Store
from app.v2_main import create_app
from app.followup.store import Repository,Conflict
from app.followup.observability import ObservedClient,calling
from app.followup.checkpoints import Checkpoints,checkpoint_call
from app.followup.task_lookup import task_lookup
from app.followup.knowledge_publish import KnowledgePublications
from app.followup.knowledge_monitor import KnowledgeMonitor
from app.followup.retrieval import build_index

OUT=ROOT/'data/reliability_quantification'/datetime.now(timezone.utc).strftime('run-%Y%m%dT%H%M%SZ')
OUT.mkdir(parents=True,exist_ok=False)
RESULT={'scope':'isolated deterministic injections; only checkpoint component cost uses live model; not production reliability or clinical evaluation','groups':{}}

def save():
    (OUT/'results.json').write_text(json.dumps(RESULT,ensure_ascii=False,indent=2),encoding='utf-8')
def check(rows,name,condition,**data):
    rows.append({'id':name,'passed':bool(condition),**data})
def finish(name,rows,**metrics):
    RESULT['groups'][name]={'passed':sum(r['passed'] for r in rows),'scenarios':len(rows),'rows':rows,**metrics};save()
    print(name+': '+str(sum(r['passed'] for r in rows))+'/'+str(len(rows)),flush=True)
def call_rows(repo,run=None):
    with repo.db() as db:
        return [{**dict(r),**json.loads(r['body'])} for r in db.execute('SELECT * FROM calls'+(' WHERE run_id=?' if run else ''),(run,) if run else ())]

async def tracing():
    repo=Repository(OUT/'trace.db');rows=[];expected=0
    for kind in ['known','unknown','retry429','http401','timeout','cancel']:
        hits=0;entered=asyncio.Event()
        async def handler(request):
            nonlocal hits
            hits+=1;entered.set()
            if kind=='cancel':await asyncio.Event().wait()
            if kind=='timeout':raise httpx.ReadTimeout('PRIVATE_RAW',request=request)
            if kind=='http401':return httpx.Response(401,json={'error':'PRIVATE_RAW'})
            if kind=='retry429' and hits==1:return httpx.Response(429,json={'error':'PRIVATE_RAW'})
            body={'choices':[{'finish_reason':'stop','message':{'content':'ok'}}]}
            if kind!='unknown':body['usage']={'prompt_tokens':11,'completion_tokens':3,'total_tokens':14}
            return httpx.Response(200,json=body)
        client=ObservedClient(Settings(deepseek_key='PRIVATE_KEY',deepseek_base='https://example.test'),repo,httpx.MockTransport(handler))
        identifier='trace-'+kind
        repo.stage(identifier,'measurement','begin',status='running')
        async def invoke():
            with calling(identifier,'measurement','fault_injection'):
                return await client.complete([{'role':'user','content':'PRIVATE_INPUT'}])
        try:
            if kind=='cancel':
                task=asyncio.create_task(invoke());await entered.wait();task.cancel()
                try:await task
                except asyncio.CancelledError:pass
            else:await invoke()
        except Exception:pass
        repo.stage(identifier,'measurement','end',status='completed')
        records=call_rows(repo,identifier);expected+=hits
        correct=(len(records)==hits and all(r['run_id']==identifier and r['duration_ms']>=0 for r in records))
        if kind=='known':correct=correct and records[0]['total_tokens']==14
        if kind=='unknown':correct=correct and records[0]['total_tokens'] is None and not records[0]['usage_known']
        if kind=='retry429':correct=correct and [r['attempt'] for r in records]==[1,2] and records[0]['error_code']=='rate_limited'
        if kind=='http401':correct=correct and records[0]['status']=='failed'
        if kind=='timeout':correct=correct and len(records)==2 and all(r['error_code']=='provider_timeout' for r in records)
        if kind=='cancel':correct=correct and records[0]['status']=='cancelled'
        correct=correct and 'PRIVATE' not in json.dumps(repo.trace(identifier))
        check(rows,kind,correct,injected_http_attempts=hits,logged_attempts=len(records),unknown_usage_calls=sum(not r.get('usage_known') for r in records))
    repo.begin_call('trace-restart','measurement','fault_injection','mock',1);repo.recover()
    check(rows,'restart',call_rows(repo,'trace-restart')[0]['status']=='interrupted')
    finish('tracing',rows,http_attempts_expected=expected,http_attempts_logged=sum(r['logged_attempts'] for r in rows if 'logged_attempts' in r),
           mode='mock HTTP transport; reported token values are synthetic, not billed tokens',stage_scope='explicitly instrumented stages only; not all workflow nodes or distributed spans')

async def recovery():
    settings=Settings();rows=[];arms={}
    if not settings.deepseek_key:
        finish('recovery',[],status='not_run_missing_model_key');return
    for use_checkpoint in [False,True]:
        name='checkpoint' if use_checkpoint else 'restart';repo=Repository(OUT/(name+'.db'));client=ObservedClient(settings,repo)
        manager=Checkpoints(repo);counts=[0,0,0];rid='cost-'+name;started=time.monotonic()
        async def pipeline(epoch):
            with manager.activate('measurement',rid,epoch) if use_checkpoint else nullcontext():
                for index in range(3):
                    async def operation(index=index):
                        counts[index]+=1
                        if epoch==0 and index==2:raise RuntimeError('injected_before_third_model_call')
                        with calling(rid,'checkpoint-cost','stage-'+str(index)):
                            response=await client.complete([{'role':'system','content':'仅输出一句简短中文，不使用工具。'},
                                {'role':'user','content':['把三个步骤概括为一句话：识别任务、查找资料、整理结果。','用一句话说明为什么要记录任务执行状态。','用一句话说明不要把未完成任务说成已完成。'][index]}],temperature=0,max_tokens=80)
                        return {'content':response['content']}
                    await checkpoint_call('measurement-stage-'+str(index),{'fixture':index},operation) if use_checkpoint else await operation()
        try:await pipeline(0)
        except RuntimeError:pass
        # Reopen the durable repository to ensure it is not an in-memory memo cache.
        if use_checkpoint:manager=Checkpoints(Repository(repo.path))
        await pipeline(1)
        records=call_rows(repo,rid)
        arms[name]={'model_http_attempts':len(records),'stage_factory_invocations':counts,'unknown_usage_calls':sum(not r.get('usage_known') for r in records),
            'input_tokens':sum(r.get('input_tokens') or 0 for r in records if r.get('usage_known')),
            'output_tokens':sum(r.get('output_tokens') or 0 for r in records if r.get('usage_known')),
            'total_tokens':sum(r.get('total_tokens') or 0 for r in records if r.get('usage_known')),
            'wall_ms':round((time.monotonic()-started)*1000),'checkpoint_reuse_events':sum(r.get('reason')=='checkpoint_reused' for r in repo.trace(rid)['stages'])}
    check(rows,'completed_stages_reused',arms['checkpoint']['stage_factory_invocations']==[1,1,2],arms=arms)
    check(rows,'baseline_reexecutes_completed_stages',arms['restart']['stage_factory_invocations']==[2,2,2])
    check(rows,'request_reduction',arms['restart']['model_http_attempts']==5 and arms['checkpoint']['model_http_attempts']==3)
    finish('recovery',rows,arms=arms,model=settings.chat_model,request_saving_fraction=1-arms['checkpoint']['model_http_attempts']/arms['restart']['model_http_attempts'],
        token_saving_fraction=(1-arms['checkpoint']['total_tokens']/arms['restart']['total_tokens']) if not any(a['unknown_usage_calls'] for a in arms.values()) else None,
        scope='single 3-stage checkpoint component trial; injected failure before stage3 HTTP request; real complete() calls but not full production hospital workflow; wall times non-comparable to production latency')

def make_document(repo,status='draft'):
    return repo.create('*','knowledge','',{'title':'isolated fixture','kind':'public','status':status,'pieces':[{'location':'body','text':'fixture source text '*12}],
        'url':'https://example.org/guide','institution':'fixture','rights':'test','published_at':'2026-10-19'})
async def prepare(doc,progress):
    payload={k:v for k,v in doc.items() if k not in {'id','revision','subject','created_at','updated_at'}}
    payload.update(status='published',vectors={});payload['retrieval_index']=build_index({**doc,**payload,'revision':doc['revision']+1});return payload

async def versions():
    rows=[]
    for change in ['edit','withdraw','delete','embedding_failure','commit_failure','success']:
        repo=Repository(OUT/('version-'+change+'.db'));doc=make_document(repo)
        async def generate(row,progress):
            payload=await prepare(row,progress)
            if change=='embedding_failure':raise RuntimeError('injected')
            if change=='delete':repo.delete('*','knowledge',doc['id'])
            if change in {'edit','withdraw'}:repo.update('*','knowledge',doc['id'],{**doc,'status':'withdrawn' if change=='withdraw' else 'draft','pieces':[{'location':'body','text':'new revision'}]},1)
            return payload
        jobs=KnowledgePublications(repo,generate);job=jobs.submit(doc)
        if change=='commit_failure':
            with repo.db() as db:db.execute("CREATE TRIGGER fail_completion BEFORE UPDATE ON knowledge_publications WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'injected'); END")
        await jobs.process_one();stored=repo.get('*','knowledge',doc['id']);state=jobs.get(job['id'])
        if change=='success':ok=state['status']=='completed' and stored['status']=='published' and 'retrieval_index' in stored
        elif change=='delete':ok=state['status']=='failed' and stored is None
        elif change in {'edit','withdraw'}:ok=state['error_code']=='publication_stale' and stored['pieces'][0]['text']=='new revision' and stored['status']!='published'
        else:ok=state['status']=='failed' and stored['status']=='draft' and stored['revision']==1 and 'retrieval_index' not in stored
        check(rows,change,ok,task_status=state['status'],document_status=stored['status'] if stored else 'deleted')
    repo=Repository(OUT/'optimistic.db');doc=make_document(repo)
    repo.update('*','knowledge',doc['id'],{**doc,'title':'winner'},1)
    blocked=False
    try:repo.update('*','knowledge',doc['id'],{**doc,'title':'loser'},1)
    except Conflict:blocked=True
    check(rows,'stale_optimistic_write',blocked and repo.get('*','knowledge',doc['id'])['title']=='winner')
    finish('versions',rows,scope='SQLite single-process controlled interleaving and commit-trigger failure; no distributed race claim')

async def permissions():
    repo=Repository(OUT/'permissions.db');accounts=Store('sqlite:///'+(OUT/'auth.db').as_posix())
    a=accounts.register('quantowner','123456');b=accounts.register('quantother','123456')
    app=create_app(Settings(mode='demo',external_enabled=False,embedding_key=''),repo,account_store=accounts)
    subject=repo.create(a['id'],'subject','',{'name':'PRIVATE_MARKER','diseases':[]})
    other_subject=repo.create(a['id'],'subject','',{'name':'second','diseases':[]})
    doc=repo.create(a['id'],'document',subject['id'],{'title':'PRIVATE_MARKER','pieces':[{'location':'body','text':'PRIVATE_MARKER'}],'confirmed':True})
    record=repo.create(a['id'],'record',subject['id'],{'label':'PRIVATE_MARKER'})
    conv=repo.create(a['id'],'conversation',subject['id'],{'title':'PRIVATE_MARKER','messages':[]})
    run=repo.create(a['id'],'run',subject['id'],{'status':'completed','result':{'content':'PRIVATE_MARKER'},'snapshot':{}})
    rows=[];transport=httpx.ASGITransport(app=app)
    cases=[('records','GET','/v2/records?subject='+subject['id'],None),('documents','GET','/v2/documents?subject='+subject['id'],None),
        ('conversation','GET','/v2/conversations/'+conv['id'],None),('run','GET','/v2/runs/'+run['id'],None),
        ('delete_document','DELETE','/v2/documents/'+doc['id'],None),('delete_record','DELETE','/v2/records/'+record['id'],None),
        ('delete_subject','DELETE','/v2/subjects/'+subject['id'],None),('cancel_run','POST','/v2/runs/'+run['id']+'/cancel',None),
        ('dashboard','GET','/v2/admin/dashboard',None),('trace','GET','/v2/admin/traces/'+run['id'],None),
        ('knowledge','GET','/v2/admin/knowledge',None),('inspections','GET','/v2/admin/knowledge-inspections',None)]
    for role in ['anonymous','other','owner']:
        async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
            if role!='anonymous':
                login=await client.post('/auth/login',json={'username':'quant'+('other' if role=='other' else 'owner'),'password':'123456'})
                assert login.status_code==200,'fixture login failed'
            for label,method,url,body in cases:
                if role=='owner' and label not in {'dashboard','trace','knowledge','inspections'}:continue
                response=await client.request(method,url,json=body)
                expected=401 if role=='anonymous' else 403 if label in {'dashboard','trace','knowledge','inspections'} else 404
                check(rows,role+'-'+label,response.status_code==expected and 'PRIVATE_MARKER' not in response.text,status=response.status_code,expected=expected)
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as owner:
        login=await owner.post('/auth/login',json={'username':'quantowner','password':'123456'})
        assert login.status_code==200,'positive control login failed'
        cross=await owner.post('/v2/runs',json={'subject':other_subject['id'],'goal':'read report','document_ids':[doc['id']]})
        check(rows,'same_owner_wrong_subject_attachment',cross.status_code==404 and 'PRIVATE_MARKER' not in cross.text,status=cross.status_code)
        positive=await owner.get('/v2/documents?subject='+subject['id'])
        check(rows,'authorized_positive_control',positive.status_code==200 and 'PRIVATE_MARKER' in positive.text)
    check(rows,'unauthorized_mutations_left_data_intact',all([repo.get(a['id'],'document',doc['id']),repo.get(a['id'],'record',record['id']),repo.get(a['id'],'subject',subject['id'])]))
    accounts.engine.dispose();finish('permissions',rows,scope='HTTP ownership/admin/subject matrix; includes authorized control; not all endpoints or prompt-injection proof')

async def monitoring():
    repo=Repository(OUT/'monitor.db');doc=make_document(repo,'published');state={'text':'original webpage content '*12,'fail':False}
    async def fetch(*args):
        if state['fail']:raise OSError('PRIVATE_UPSTREAM')
        return {'url':doc['url'],'text':state['text'],'reader':'injected','collected_at':'2026-10-19','published_at':''}
    jobs=KnowledgeMonitor(repo,fetch,lambda payload:None);rows=[]
    async def collect():
        job=jobs.submit([doc['id']])[0];await jobs.process_one();return jobs.get(job['id'],True)
    baseline=await collect();check(rows,'first_capture_baseline',baseline['outcome']=='baseline_created')
    unchanged=await collect();check(rows,'same_text',unchanged['outcome']=='unchanged')
    state['text']='\n  '+state['text']+'  \n';space=await collect();check(rows,'whitespace_only',space['outcome']=='unchanged')
    state['text']='updated webpage content '*12;changed=await collect();check(rows,'actual_text_change',changed['outcome']=='changed')
    check(rows,'no_automatic_overwrite',repo.get('*','knowledge',doc['id'])==doc)
    with repo.db() as db:before=dict(db.execute('SELECT * FROM knowledge_monitor_baselines').fetchone())
    state['fail']=True;failed=await collect()
    with repo.db() as db:after=dict(db.execute('SELECT * FROM knowledge_monitor_baselines').fetchone())
    check(rows,'failed_read_preserves_baseline',failed['status']=='failed' and before==after and 'PRIVATE' not in json.dumps(failed))
    state['fail']=False;jobs.retry(failed['id']);await jobs.process_one()
    check(rows,'failed_read_retry',jobs.get(failed['id'])['status']=='completed')
    jobs.review(changed['id'],'dismiss');check(rows,'dismiss_keeps_publication',repo.get('*','knowledge',doc['id'])==doc)
    current=await collect();jobs.review(current['id'],'apply');draft=repo.get('*','knowledge',doc['id'])
    check(rows,'adopt_pauses_publication',draft['status']=='draft' and draft['revision']==2 and 'retrieval_index' not in draft)
    with repo.db() as db:archive=db.execute('SELECT original_json FROM knowledge_monitor_versions WHERE job_id=?',(current['id'],)).fetchone()
    check(rows,'old_content_archived',json.loads(archive['original_json'])['pieces']==doc['pieces'])
    stale=False
    try:jobs.review(baseline['id'],'apply')
    except Conflict:stale=True
    check(rows,'stale_snapshot_blocked',stale)
    job=KnowledgePublications(repo,prepare).submit(draft);publishing=KnowledgePublications(repo,prepare);await publishing.process_one()
    check(rows,'explicit_republication',publishing.get(job['id'])['status']=='completed' and repo.get('*','knowledge',doc['id'])['status']=='published')
    finish('monitoring',rows,text_comparison_samples=4,text_comparison_correct=sum(r['passed'] for r in rows[:4]),
        scope='injected webpage text; measures literal normalized-text changes and state transitions, not semantic change or real hospital crawl accuracy; republish uses injected prepare')

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--groups',nargs='+',choices=['tracing','recovery','versions','permissions','monitoring']);args=parser.parse_args()
    print(str(OUT),flush=True)
    for name,function in [('tracing',tracing),('recovery',recovery),('versions',versions),('permissions',permissions),('monitoring',monitoring)]:
        if args.groups and name not in args.groups:continue
        try:await function()
        except Exception as error:
            RESULT['groups'][name]={'status':'execution_error','error_type':type(error).__name__};save();print(name+' ERROR '+type(error).__name__,flush=True);raise
    print('Completed isolated measurements',flush=True)
if __name__=='__main__':asyncio.run(main())
