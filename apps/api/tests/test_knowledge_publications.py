import asyncio

import httpx
import pytest

from app.config import Settings
from app.storage import Store
from app.v2_main import create_app
from app.followup.store import Repository, Conflict
from app.followup.retrieval import build_index, chunks
from app.followup.knowledge_publish import KnowledgePublications


def document(repo):
    return repo.create('*','knowledge','',{'title':'合成发布资料','kind':'public','status':'draft',
        'pieces':[{'location':'第1页','text':'合成紫檀桥上午开放。'}],
        'url':'https://example.org/synthetic','institution':'合成机构','rights':'合成测试',
        'published_at':'2026-10-19'})


async def prepare(row,progress):
    progress('chunking',0,1)
    payload={k:v for k,v in row.items() if k not in {'id','revision','subject','created_at','updated_at'}}
    payload.update(status='published',vectors={})
    payload['retrieval_index']=build_index({**row,**payload,'revision':row['revision']+1})
    return payload


def test_persistence_duplicate_recovery_and_atomic_commit(tmp_path):
    repo=Repository(tmp_path/'business.db');row=document(repo)
    jobs=KnowledgePublications(repo,prepare)
    task=jobs.submit(row)
    assert jobs.submit(row)['id']==task['id']
    with repo.db() as db:
        db.execute("UPDATE knowledge_publications SET status='processing',attempts=1 WHERE id=?",(task['id'],))
    reopened=KnowledgePublications(Repository(tmp_path/'business.db'),prepare)
    reopened.recover()
    assert reopened.get(task['id'])['error_code']=='worker_interrupted'
    reopened.retry(task['id']);asyncio.run(reopened.process_one())
    assert reopened.get(task['id'])['status']=='completed'
    result=repo.get('*','knowledge',row['id'])
    assert result['status']=='published' and result['revision']==2
    assert chunks(result)[0]['revision']==2
    assert reopened.get(task['id'])['attempts']==2


@pytest.mark.parametrize('change',['edit','withdraw','delete'])
def test_inflight_version_change_cannot_publish_old_content(tmp_path,change):
    async def scenario():
        repo=Repository(tmp_path/'business.db');row=document(repo)
        async def prepare_and_change(doc,progress):
            payload=await prepare(doc,progress)
            if change=='delete':repo.delete('*','knowledge',row['id'])
            else:repo.update('*','knowledge',row['id'],{**row,'status':'withdrawn' if change=='withdraw' else 'draft',
                                 'pieces':[{'location':'正文','text':'最新内容'}]},1)
            return payload
        jobs=KnowledgePublications(repo,prepare_and_change);task=jobs.submit(row)
        await jobs.process_one()
        assert jobs.get(task['id'])['error_code']=='publication_stale'
        result=repo.get('*','knowledge',row['id'])
        assert result is None if change=='delete' else result['status']!='published' and result['pieces'][0]['text']=='最新内容'
        with pytest.raises(Conflict):jobs.retry(task['id'])
    asyncio.run(scenario())


def test_failed_publication_retry_and_no_partial_index(tmp_path):
    repo=Repository(tmp_path/'business.db');row=document(repo)
    async def unavailable(doc,progress):
        progress('embedding',1,2)
        raise RuntimeError('PRIVATE UPSTREAM RESPONSE MUST NOT LEAK')
    jobs=KnowledgePublications(repo,unavailable);task=jobs.submit(row)
    asyncio.run(jobs.process_one())
    assert jobs.get(task['id'])['error_code']=='publication_failed'
    stored=repo.get('*','knowledge',row['id'])
    assert stored['status']=='draft' and 'retrieval_index' not in stored
    assert 'PRIVATE' not in str(jobs.items())
    for _ in range(2):jobs.retry(task['id']);asyncio.run(jobs.process_one())
    with pytest.raises(Conflict):jobs.retry(task['id'])


def test_publication_completion_rolls_back_with_task_update(tmp_path):
    repo=Repository(tmp_path/'business.db');row=document(repo)
    jobs=KnowledgePublications(repo,prepare);task=jobs.submit(row)
    with repo.db() as db:
        db.execute("CREATE TRIGGER fail_completion BEFORE UPDATE ON knowledge_publications WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    asyncio.run(jobs.process_one())
    stored=repo.get('*','knowledge',row['id'])
    assert stored['status']=='draft' and stored['revision']==1
    assert jobs.get(task['id'])['status']=='failed'
    with repo.db() as db:db.execute('DROP TRIGGER fail_completion')
    jobs.retry(task['id']);asyncio.run(jobs.process_one())
    assert repo.get('*','knowledge',row['id'])['status']=='published'


def test_http_queue_permissions_validation_and_vector_progress(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('publishadmin','test123456',role='admin')
        class Model:
            started=asyncio.Event();release=asyncio.Event()
            async def embed(self,texts):
                self.started.set();await self.release.wait()
                return [[1.,0.] for _ in texts]
        model=Model()
        app=create_app(Settings(mode='live',external_enabled=False,embedding_key='synthetic',dimensions=2),repo,model,accounts)
        row=document(repo)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                endpoint='/v2/admin/knowledge/'+row['id']+'/publish-task'
                assert (await client.get('/v2/admin/knowledge-publications')).status_code==401
                await client.post('/auth/login',json={'username':'publishadmin','password':'test123456'})
                response=await client.post(endpoint,json={'revision':1})
                assert response.status_code==202,response.text
                task=response.json()
                await asyncio.wait_for(model.started.wait(),3)
                assert repo.get('*','knowledge',row['id'])['status']=='draft'
                listed=(await client.get('/v2/admin/knowledge-publications')).json()['items']
                assert listed[0]['stage']=='embedding' and listed[0]['total_chunks']==1
                assert (await client.post(endpoint,json={'revision':1})).json()['id']==task['id']
                model.release.set()
                for _ in range(100):
                    listed=(await client.get('/v2/admin/knowledge-publications')).json()['items']
                    if listed[0]['status']=='completed':break
                    await asyncio.sleep(.02)
                assert listed[0]['status']=='completed'
                stored=repo.get('*','knowledge',row['id'])
                assert stored['status']=='published' and chunks(stored)[0]['embedding']==[1.,0.]
                assert (await client.post(endpoint,json={'revision':1})).status_code==409
                invalid=repo.create('*','knowledge','',{'title':'未核对来源','pieces':[{'location':'正文','text':'合成资料'}],'status':'draft'})
                assert (await client.post('/v2/admin/knowledge/'+invalid['id']+'/publish-task',json={'revision':1})).status_code==422
                await client.post('/auth/register',json={'username':'publishuser','password':'test123456'})
                assert (await client.get('/v2/admin/knowledge-publications')).status_code==403
                assert (await client.post(endpoint,json={'revision':2})).status_code==403
                assert (await client.post('/v2/admin/knowledge-publications/'+task['id']+'/retry')).status_code==403
        accounts.engine.dispose()
    asyncio.run(scenario())
