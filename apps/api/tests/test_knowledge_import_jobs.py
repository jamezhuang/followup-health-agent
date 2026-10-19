"""Durable queue and HTTP tests; all files and accounts are synthetic."""
import asyncio

import httpx
import pytest

from app.config import Settings
from app.storage import Store
from app.v2_main import create_app, Document
from app.followup.store import Repository, Conflict
from app.followup.knowledge_jobs import KnowledgeImports


def validate(name,pieces):
    return {**Document(title=name,pieces=pieces).model_dump(exclude={'subject','revision'}),
            'kind':'public','status':'draft'}


def test_queue_persistence_recovery_and_atomic_completion(tmp_path):
    repo = Repository(tmp_path/'business.db')
    jobs = KnowledgeImports(repo,validate)
    task = jobs.submit('guide.txt','合成入库资料。'.encode())
    assert task['status']=='queued' and 'data' not in task
    assert jobs.submit('renamed.txt','合成入库资料。'.encode())['id']==task['id']
    assert not repo.items('*','knowledge')
    # Another repository/service instance sees the persisted upload.
    reopened = KnowledgeImports(Repository(tmp_path/'business.db'),validate)
    claimed = reopened.claim()
    assert claimed['data']=='合成入库资料。'.encode()
    reopened.recover()
    assert reopened.get(task['id'])['error_code']=='worker_interrupted'
    reopened.retry(task['id'])
    assert asyncio.run(reopened.process_one())
    done = reopened.get(task['id'])
    assert done['status']=='completed' and done['attempts']==2
    doc = repo.get('*','knowledge',done['document_id'])
    assert doc['status']=='draft' and repo.knowledge_file(doc['id'])['data']==claimed['data']
    with repo.db() as db:
        assert db.execute('SELECT data FROM knowledge_imports WHERE id=?',(task['id'],)).fetchone()[0] is None
    assert reopened.submit('guide.txt',claimed['data'])['document_id']==doc['id']
    assert len(repo.items('*','knowledge'))==1
    with pytest.raises(Conflict): reopened.retry(task['id'])
    repo.delete('*','knowledge',doc['id'])
    assert reopened.submit('guide.txt',claimed['data'])['id']!=task['id']


def test_failures_retry_limit_and_parse_failure_has_no_draft(tmp_path,monkeypatch):
    import app.followup.knowledge_jobs as module
    repo = Repository(tmp_path/'business.db')
    jobs = KnowledgeImports(repo,validate)
    task = jobs.submit('bad.txt',b'\xff')
    asyncio.run(jobs.process_one())
    assert jobs.get(task['id'])['error_code']=='utf8_required'
    assert not repo.items('*','knowledge') and repo.knowledge_file(task['id']) is None
    for _ in range(2):
        jobs.retry(task['id']); asyncio.run(jobs.process_one())
    with pytest.raises(Conflict): jobs.retry(task['id'])
    transient = jobs.submit('valid.txt',b'synthetic public text')
    parser = module.parse_file
    def fail(*args): raise OSError('SYNTHETIC PRIVATE EXCEPTION MUST NOT LEAK')
    monkeypatch.setattr(module,'parse_file',fail)
    asyncio.run(jobs.process_one())
    assert jobs.get(transient['id'])['error_code']=='import_failed'
    assert 'PRIVATE' not in str(jobs.items())
    monkeypatch.setattr(module,'parse_file',parser)
    jobs.retry(transient['id']); asyncio.run(jobs.process_one())
    assert jobs.get(transient['id'])['status']=='completed'
    assert len(repo.items('*','knowledge'))==1


def test_complete_transaction_rolls_back_on_storage_error(tmp_path):
    repo = Repository(tmp_path/'business.db')
    jobs = KnowledgeImports(repo,validate)
    task = jobs.submit('guide.txt',b'synthetic')
    jobs.claim()
    # A storage error after entity insertion must roll the entire transaction back.
    with repo.db() as db:
        db.execute("CREATE TRIGGER fail_original BEFORE INSERT ON knowledge_files BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        raw = dict(db.execute('SELECT * FROM knowledge_imports WHERE id=?',(task['id'],)).fetchone())
    with pytest.raises(Exception): jobs.complete(raw,validate('guide.txt',[{'location':'正文','text':'synthetic'}]))
    assert repo.items('*','knowledge')==[]
    assert jobs.get(task['id'])['status']=='processing'


def test_queue_bound(tmp_path):
    jobs = KnowledgeImports(Repository(tmp_path/'business.db'),validate)
    for i in range(20): jobs.submit('file.txt',str(i).encode())
    with pytest.raises(Conflict): jobs.submit('file.txt',b'overflow')
    assert len(jobs.items())==20


def test_async_http_permissions_result_and_reload(tmp_path):
    async def scenario():
        repo = Repository(tmp_path/'business.db')
        accounts = Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('queueadmin','test123456',role='admin')
        app = create_app(Settings(mode='demo',embedding_key='',external_enabled=False),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                assert (await client.get('/v2/admin/knowledge-imports')).status_code==401
                await client.post('/auth/login',json={'username':'queueadmin','password':'test123456'})
                response = await client.post('/v2/admin/knowledge-imports',files={'file':('guide.txt','合成异步资料。'.encode())})
                assert response.status_code==202
                task = response.json()
                for _ in range(100):
                    listed = (await client.get('/v2/admin/knowledge-imports')).json()['items']
                    current = next(row for row in listed if row['id']==task['id'])
                    if current['status'] in {'completed','failed'}: break
                    await asyncio.sleep(.02)
                assert current['status']=='completed',current
                assert repo.get('*','knowledge',current['document_id'])['status']=='draft'
                repeated = await client.post('/v2/admin/knowledge-imports',files={'file':('guide.txt','合成异步资料。'.encode())})
                assert repeated.json()['id']==task['id'] and len(repo.items('*','knowledge'))==1
                assert (await client.post('/v2/admin/knowledge-imports/'+task['id']+'/retry')).status_code==409
                assert (await client.post('/v2/admin/knowledge-imports',files={'file':('x.exe',b'x')})).status_code==422
                assert (await client.post('/v2/admin/knowledge-imports',files={'file':('x.txt',b'')})).status_code==422
                await client.post('/auth/register',json={'username':'queueuser','password':'test123456'})
                assert (await client.get('/v2/admin/knowledge-imports')).status_code==403
                assert (await client.post('/v2/admin/knowledge-imports',files={'file':('x.txt',b'x')})).status_code==403
                assert (await client.post('/v2/admin/knowledge-imports/'+task['id']+'/retry')).status_code==403
        # Task result and original still exist after a second app lifespan starts.
        app = create_app(Settings(mode='demo',embedding_key='',external_enabled=False),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            jobs = KnowledgeImports(repo,validate)
            assert jobs.get(task['id'])['status']=='completed'
        accounts.engine.dispose()
    asyncio.run(scenario())
