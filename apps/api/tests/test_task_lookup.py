import asyncio,json
import httpx
import pytest
from app.config import Settings
from app.storage import Store
from app.v2_main import create_app
from app.followup.store import Repository,Conflict
from app.followup.task_lookup import task_lookup,usage_summary
from app.followup.observability import ObservedClient


def test_accounting_includes_retry_but_never_invents_unknown_usage():
    result=usage_summary([{'total_tokens':12,'input_tokens':10,'output_tokens':2,'usage_known':True,'duration_ms':100},
        {'total_tokens':None,'duration_ms':200,'status':'failed'},
        {'total_tokens':20,'usage_known':True,'duration_ms':None}])
    assert result['calls']==3 and result['known_total_tokens']==32
    assert result['unknown_usage_calls']==1 and result['unknown_duration_calls']==1
    assert result['model_request_sum_ms']==300 and result['price_status']=='not_calculated'


def test_exact_lookup_not_limited_to_recent_100_and_no_private_content(tmp_path):
    repo=Repository(tmp_path/'business.db')
    target=repo.create('SECRET OWNER','run','SECRET PATIENT',{'status':'completed','goal':'SECRET QUESTION',
        'result':{'content':'SECRET ANSWER','citation_review':{'status':'passed'}}})
    for _ in range(101):repo.create('x','run','',{'status':'completed'})
    first=repo.begin_call(target['id'],'agent','test','model',1)
    repo.end_call(first,status='failed',duration_ms=3,total_tokens=None,failure_reason='SECRET EXCEPTION')
    second=repo.begin_call(target['id'],'agent','test','model',2)
    repo.end_call(second,status='ok',duration_ms=5,total_tokens=7,usage_known=True)
    result=task_lookup(repo,target['id'])
    assert result['usage']['calls']==2 and result['usage']['known_total_tokens']==7
    assert result['usage']['unknown_usage_calls']==1
    assert 'SECRET' not in json.dumps(result)
    assert task_lookup(repo,target['id'][:12]) is None


def test_lookup_api_permissions_collision_and_missing(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('lookupadmin','test123456',role='admin')
        app=create_app(Settings(mode='demo',embedding_key='',external_enabled=False),repo,account_store=accounts)
        row=repo.create('x','run','',{'status':'completed'})
        with repo.db() as db:
            db.execute("INSERT INTO knowledge_imports VALUES (?, 'SECRET FILENAME', 'hash', ?, 'failed','failed','parse_failed','',1,'2026-10-19','2026-10-19')",(row['id'],b'SECRET BYTES'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            endpoint='/v2/admin/tasks/'+row['id']
            assert (await client.get(endpoint)).status_code==401
            await client.post('/auth/login',json={'username':'lookupadmin','password':'test123456'})
            assert (await client.get(endpoint)).status_code==409
            response=await client.get(endpoint+'?kind=import')
            assert response.status_code==200 and response.json()['task']['type']=='import'
            assert 'SECRET' not in response.text
            assert (await client.get('/v2/admin/tasks/not-found')).status_code==404
            assert (await client.get(endpoint+'?kind=invalid')).status_code==422
            await client.post('/auth/register',json={'username':'lookupuser','password':'test123456'})
            assert (await client.get(endpoint)).status_code==403
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_new_publication_usage_is_attributed_to_task_not_document(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('publicationusage','test123456',role='admin')
        settings=Settings(mode='live',embedding_key='synthetic',dimensions=2,external_enabled=False)
        def upstream(request):
            body=json.loads(request.content)
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1.,0.]} for i in range(len(body['input']))],
                                          'usage':{'prompt_tokens':17,'total_tokens':17}})
        client=ObservedClient(settings,repo,httpx.MockTransport(upstream))
        app=create_app(settings,repo,client,accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
                await api.post('/auth/login',json={'username':'publicationusage','password':'test123456'})
                document=(await api.post('/v2/admin/knowledge',json={'title':'合成资料','pieces':[{'location':'正文','text':'合成内容'}],
                    'institution':'合成机构','url':'https://example.org','rights':'合成验收','published_at':'2026-10-19'})).json()
                task=(await api.post('/v2/admin/knowledge/'+document['id']+'/publish-task',json={'revision':1})).json()
                for _ in range(100):
                    result=(await api.get('/v2/admin/tasks/'+task['id']+'?kind=publication')).json()
                    if result['task']['status']=='completed':break
                    await asyncio.sleep(.02)
                assert result['task']['status']=='completed',result
                assert result['usage']['known_total_tokens']==17 and result['usage']['calls']==1
                legacy=repo.begin_call('knowledge-'+document['id'],'agent','test','model',1)
                repo.end_call(legacy,total_tokens=999,usage_known=True,status='ok')
                result=(await api.get('/v2/admin/tasks/'+task['id']+'?kind=publication')).json()
                assert result['legacy_document_calls_present']
                assert result['usage']['known_total_tokens']==17
                assert '_publication_task_id' not in repo.get('*','knowledge',document['id'])
        accounts.engine.dispose()
    asyncio.run(scenario())
