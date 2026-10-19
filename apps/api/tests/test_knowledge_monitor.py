import asyncio,json
import httpx
import pytest
from app.config import Settings
from app.storage import Store
from app.v2_main import create_app
from app.followup.store import Repository,Conflict
from app.followup.knowledge_monitor import KnowledgeMonitor
from app.followup.task_lookup import task_lookup


OLD='原网页正文，开放时间为上午九时。\n'*12
NEW='新网页正文，开放时间为下午三时。\n'*12


def document(repo):
    return repo.create('*','knowledge','',{'title':'合成公共来源','url':'https://example.org/guide',
        'institution':'合成机构','kind':'public','status':'published','published_at':'2026-10-18',
        'rights':'合成验收','pieces':[{'location':'正文节选','text':'原知识库仅为节选，不是整页。'}],
        'vectors':{'fixture':[1.,0.]},'retrieval_index':{'fixture':True}})


def service(tmp_path):
    repo=Repository(tmp_path/'business.db');doc=document(repo)
    state={'text':OLD,'fail':False,'url':'https://example.org/guide'}
    async def fetch(*args):
        if state['fail']:raise OSError('PRIVATE UPSTREAM EXCEPTION')
        return {'text':state['text'],'url':state['url'],'reader':'synthetic','authority':'unverified',
                'published_at':'','collected_at':'2026-10-19'}
    return repo,doc,state,KnowledgeMonitor(repo,fetch,lambda payload:None)


def inspect(jobs,doc):
    task=jobs.submit([doc['id']])[0];asyncio.run(jobs.process_one());return jobs.get(task['id'],True)


def test_first_baseline_is_not_a_claim_of_changed_knowledge_and_never_auto_updates(tmp_path):
    repo,doc,state,jobs=service(tmp_path)
    task=inspect(jobs,doc)
    assert task['outcome']=='baseline_created' and task['before_text']==''
    assert task['published_text']==doc['pieces'][0]['text']
    assert repo.get('*','knowledge',doc['id'])==doc
    assert 'after_json' not in jobs.items()[0] and 'snapshot' not in jobs.items()[0]
    assert task_lookup(repo,task['id'])['task']['type']=='inspection'
    state['text']='\n  '+OLD.replace('\n','  \n\n')
    assert inspect(jobs,doc)['outcome']=='unchanged'
    state['text']=NEW
    changed=inspect(jobs,doc)
    assert changed['outcome']=='changed' and '原网页正文' in changed['before_text']
    assert repo.get('*','knowledge',doc['id'])['status']=='published'


def test_adoption_archives_old_text_clears_index_and_requires_republication(tmp_path):
    repo,doc,state,jobs=service(tmp_path)
    task=inspect(jobs,doc)
    jobs.review(task['id'],'apply')
    current=repo.get('*','knowledge',doc['id'])
    assert current['status']=='draft' and current['revision']==2
    assert current['published_at']=='' and current['collected_at']=='2026-10-19'
    assert 'vectors' not in current and 'retrieval_index' not in current
    assert '原网页正文' in current['pieces'][0]['text']
    with repo.db() as db:
        archived=db.execute('SELECT * FROM knowledge_monitor_versions WHERE job_id=?',(task['id'],)).fetchone()
    assert json.loads(archived['original_json'])['pieces']==doc['pieces'] and archived['revision']==1
    with pytest.raises(Conflict):jobs.review(task['id'],'apply')


def test_dismiss_keeps_published_content(tmp_path):
    repo,doc,state,jobs=service(tmp_path);task=inspect(jobs,doc)
    jobs.review(task['id'],'dismiss')
    assert repo.get('*','knowledge',doc['id'])==doc
    assert jobs.get(task['id'])['review_state']=='dismissed'
    later=inspect(jobs,doc)
    assert later['outcome']=='unchanged'
    assert jobs.review(later['id'],'apply')['status']=='draft'


def test_read_failure_does_not_advance_baseline_and_can_retry(tmp_path):
    repo,doc,state,jobs=service(tmp_path);inspect(jobs,doc)
    state.update(text=NEW,fail=True)
    failed=inspect(jobs,doc)
    assert failed['status']=='failed' and failed['error_code']=='source_read_failed'
    assert 'PRIVATE' not in json.dumps(failed)
    state['fail']=False;jobs.retry(failed['id']);asyncio.run(jobs.process_one())
    result=jobs.get(failed['id'],True)
    assert result['outcome']=='changed' and '原网页正文' in result['before_text']
    assert repo.get('*','knowledge',doc['id'])==doc


@pytest.mark.parametrize('change',['edit','withdraw','delete'])
def test_stale_inspection_cannot_update_changed_document(tmp_path,change):
    repo,doc,state,jobs=service(tmp_path);task=inspect(jobs,doc)
    if change=='delete':repo.delete('*','knowledge',doc['id'])
    else:repo.update('*','knowledge',doc['id'],{**doc,'status':'withdrawn' if change=='withdraw' else 'draft'},1)
    with pytest.raises(Conflict):jobs.review(task['id'],'apply')


def test_newer_snapshot_blocks_adoption_of_older_snapshot(tmp_path):
    repo,doc,state,jobs=service(tmp_path);older=inspect(jobs,doc)
    state['text']=NEW;newer=inspect(jobs,doc)
    with pytest.raises(Conflict):jobs.review(older['id'],'apply')
    assert jobs.review(newer['id'],'apply')['status']=='draft'


def test_host_change_short_text_and_retry_boundaries(tmp_path):
    repo,doc,state,jobs=service(tmp_path)
    state['url']='https://different.example/guide'
    bad=inspect(jobs,doc)
    assert bad['error_code']=='source_host_changed'
    state.update(url=doc['url'],text='too short')
    jobs.retry(bad['id']);asyncio.run(jobs.process_one())
    assert jobs.get(bad['id'])['error_code']=='snapshot_size'
    jobs.retry(bad['id']);asyncio.run(jobs.process_one())
    with pytest.raises(Conflict):jobs.retry(bad['id'])


def test_active_task_dedup_queue_atomicity_and_recovery(tmp_path):
    repo,doc,state,jobs=service(tmp_path)
    first=jobs.submit([doc['id']])[0]
    assert jobs.submit([doc['id']])[0]['id']==first['id']
    with repo.db() as db:db.execute("UPDATE knowledge_monitor_jobs SET status='processing',attempts=1 WHERE id=?",(first['id'],))
    jobs.recover();assert jobs.get(first['id'])['error_code']=='worker_interrupted'
    jobs.retry(first['id']);asyncio.run(jobs.process_one())
    assert jobs.get(first['id'])['status']=='completed'
    drafts=repo.create('*','knowledge','',{'title':'未发布','status':'draft','url':'https://example.org'})
    n=len(jobs.items())
    with pytest.raises(Conflict):jobs.submit([doc['id'],drafts['id']])
    assert len(jobs.items())==n


def test_api_permissions_and_external_disabled_never_modify_publication(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db');doc=document(repo)
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('monitoradmin','test123456',role='admin')
        app=create_app(Settings(mode='demo',external_enabled=False,embedding_key=''),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                assert (await client.get('/v2/admin/knowledge-inspections')).status_code==401
                await client.post('/auth/login',json={'username':'monitoradmin','password':'test123456'})
                response=await client.post('/v2/admin/knowledge-inspections',json={'document_ids':[doc['id']]})
                assert response.status_code==202,response.text
                identifier=response.json()['items'][0]['id']
                for _ in range(100):
                    task=(await client.get('/v2/admin/knowledge-inspections/'+identifier)).json()
                    if task['status']=='failed':break
                    await asyncio.sleep(.02)
                assert task['error_code']=='external_disabled'
                assert repo.get('*','knowledge',doc['id'])==doc
                assert (await client.get('/v2/admin/tasks/'+identifier+'?kind=inspection')).status_code==200
                assert (await client.post('/v2/admin/knowledge-inspections/'+identifier+'/review',json={'action':'apply'})).status_code==409
                await client.post('/auth/register',json={'username':'monitoruser','password':'test123456'})
                assert (await client.get('/v2/admin/knowledge-inspections/'+identifier)).status_code==403
                assert (await client.post('/v2/admin/knowledge-inspections',json={'document_ids':[doc['id']]})).status_code==403
                assert (await client.post('/v2/admin/knowledge-inspections/'+identifier+'/review',json={'action':'apply'})).status_code==403
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_http_monitor_read_compare_review_and_republish_boundary(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import app.web_tools as module
    state={'text':OLD}
    class Tools:
        def __init__(self,*args):self.policy=SimpleNamespace(classify=lambda url:'unverified')
        def put(self,item):return {'candidate_id':'synthetic'}
        async def load(self,*args,**kwargs):return {'url':'https://example.org/guide','text':state['text'],'reader':'synthetic'},False
    monkeypatch.setattr(module,'WebTools',Tools)
    async def scenario():
        repo=Repository(tmp_path/'business.db');doc=document(repo)
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('monitorreview','test123456',role='admin')
        app=create_app(Settings(mode='live',external_enabled=True,embedding_key=''),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                await client.post('/auth/login',json={'username':'monitorreview','password':'test123456'})
                async def collect():
                    response=await client.post('/v2/admin/knowledge-inspections',json={'document_ids':[doc['id']]})
                    assert response.status_code==202,response.text
                    identifier=response.json()['items'][0]['id']
                    for _ in range(100):
                        item=(await client.get('/v2/admin/knowledge-inspections/'+identifier)).json()
                        if item['status'] in {'completed','failed'}:break
                        await asyncio.sleep(.02)
                    assert item['status']=='completed',item
                    return item
                first=await collect();assert first['outcome']=='baseline_created'
                state['text']=NEW
                changed=await collect();assert changed['outcome']=='changed'
                result=await client.post('/v2/admin/knowledge-inspections/'+changed['id']+'/review',json={'action':'apply'})
                assert result.status_code==200,result.text
                current=repo.get('*','knowledge',doc['id'])
                assert current['status']=='draft' and current['published_at']==''
                # The new source had no publication date: do not inherit/invent the old date.
                assert (await client.post('/v2/admin/knowledge/'+doc['id']+'/publish',json={'revision':current['revision']})).status_code==422
                assert (await client.get('/v2/admin/tasks/'+changed['id']+'?kind=inspection')).json()['usage']['calls']==0
        accounts.engine.dispose()
    asyncio.run(scenario())
