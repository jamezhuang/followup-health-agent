"""HTTP contracts and side-effect checks, separate from model answer quality."""
import asyncio
from contextlib import asynccontextmanager
import httpx
import pytest
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.v2_main import create_app

@asynccontextmanager
async def session(tmp_path):
    repo=Repository(tmp_path/'business.sqlite3');accounts=Store('sqlite:///'+(tmp_path/'accounts.sqlite3').as_posix())
    app=create_app(Settings(mode='demo',external_enabled=False,embedding_key='',recognition_provider='disabled'),repo,account_store=accounts)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
                yield api,repo
    finally:accounts.engine.dispose()

@pytest.mark.parametrize('payload',[{}, {'goal':''}, {'goal':'x'*4001}, {'goal':'hello','document_ids':'bad-type'}])
def test_invalid_question_is_rejected_without_creating_run(tmp_path,payload):
    async def check():
        async with session(tmp_path) as (api,repo):
            (await api.post('/auth/register',json={'username':'contract01','password':'test123456'})).raise_for_status()
            owner=(await api.get('/auth/me')).json()['user']['id']
            response=await api.post('/v2/runs',json=payload)
            assert response.status_code==422
            assert isinstance(response.json()['detail'],list)
            assert repo.items(owner,'run')==[]
    asyncio.run(check())

def test_anonymous_requests_and_other_accounts_cannot_read_private_resources(tmp_path):
    async def check():
        async with session(tmp_path) as (api,repo):
            assert (await api.get('/v2/subjects')).status_code==401
            (await api.post('/auth/register',json={'username':'contract02','password':'test123456'})).raise_for_status()
            father=(await api.post('/v2/subjects',json={'name':'父亲','diseases':[]})).json()
            doc=(await api.post('/v2/documents',json={'subject':father['id'],'title':'合成资料','confirmed':True,'pieces':[{'location':'正文','text':'PRIVATE_CONTRACT_MARKER'}]})).json()
            api.cookies.clear()
            (await api.post('/auth/register',json={'username':'contract03','password':'test123456'})).raise_for_status()
            for path in ['/v2/subjects']:
                response=await api.get(path)
                assert response.status_code==200
                assert 'PRIVATE_CONTRACT_MARKER' not in response.text
                assert father['id'] not in response.text and doc['id'] not in response.text
            response=await api.get('/v2/documents',params={'subject':father['id']})
            assert response.status_code==404 and 'PRIVATE_CONTRACT_MARKER' not in response.text
            response=await api.put('/v2/documents/'+doc['id'],json={'subject':father['id'],'title':'试图修改','confirmed':True,'revision':doc['revision'],'pieces':[{'location':'正文','text':'changed'}]})
            assert response.status_code==404
            response=await api.post('/v2/runs',json={'subject':father['id'],'goal':'读取报告'})
            assert response.status_code==404
    asyncio.run(check())

@pytest.mark.parametrize('confirmed,wrong_subject,expected',[(False,False,422),(True,True,404)])
def test_disallowed_report_does_not_start_task_or_modify_document(tmp_path,confirmed,wrong_subject,expected):
    async def check():
        async with session(tmp_path) as (api,repo):
            (await api.post('/auth/register',json={'username':'contract04','password':'test123456'})).raise_for_status()
            owner=(await api.get('/auth/me')).json()['user']['id']
            ids=[]
            for name in ['父亲','母亲']:
                ids.append((await api.post('/v2/subjects',json={'name':name,'diseases':[]})).json()['id'])
            doc=(await api.post('/v2/documents',json={'subject':ids[0],'title':'合成资料','confirmed':confirmed,'pieces':[{'location':'正文','text':'PRIVATE_CONTRACT_MARKER'}]})).json()
            before=repo.get(owner,'document',doc['id'])
            response=await api.post('/v2/runs',json={'subject':ids[1] if wrong_subject else ids[0],'goal':'解释报告','document_ids':[doc['id']]})
            assert response.status_code==expected
            assert repo.items(owner,'run')==[]
            assert repo.get(owner,'document',doc['id'])==before
    asyncio.run(check())
