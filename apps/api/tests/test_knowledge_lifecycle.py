"""Isolated public corpus lifecycle: no network, production data or medical inference."""
import asyncio
from types import SimpleNamespace

import httpx

from app.config import Settings
from app.storage import Store
from app.v2_main import create_app
from app.followup.store import Repository
from app.followup.retrieval import chunks, build_index, public_chunks
from app.followup.public_rag import public_sources


def test_file_to_published_search_edit_withdraw_and_original(tmp_path):
    async def scenario():
        repo = Repository(tmp_path/'business.db')
        accounts = Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('lifecycleadmin','test123456',role='admin')
        app = create_app(Settings(mode='demo',external_enabled=False,embedding_key=''),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                # Permission is checked before accepting an upload.
                assert (await client.post('/v2/admin/knowledge/import',files={'file':('x.txt',b'x')})).status_code==401
                await client.post('/auth/login',json={'username':'lifecycleadmin','password':'test123456'})
                text = '合成资料，芦苇渡口开放时间为上午九时。'
                upload = {'file':('public.txt',text.encode(),'text/plain')}
                response = await client.post('/v2/admin/knowledge/import',files=upload)
                assert response.status_code==200,response.text
                row = response.json(); identifier = row['id']; path = '/v2/admin/knowledge/'+identifier
                assert row['status']=='draft' and row['file_sha256'] and row['original_filename']=='public.txt'
                assert (await client.get(path+'/original')).content==text.encode()
                assert (await client.post('/v2/admin/knowledge/import',files=upload)).status_code==409
                assert len(repo.items('*','knowledge'))==1
                assert (await client.post('/v2/admin/knowledge/search',json={'query':'芦苇渡口'})).json()['items']==[]
                preview = (await client.get(path+'/chunks')).json()
                assert preview['total']==1 and 'embedding' not in preview['items'][0]
                assert (await client.post(path+'/publish',json={'revision':1})).status_code==422
                body = {'title':'合成开放指南','pieces':row['pieces'],'url':'https://example.org/guide',
                        'institution':'合成机构','rights':'合成验收资料','published_at':'2026-10-19','revision':1}
                row = (await client.put(path,json=body)).json()
                response = await client.post(path+'/publish',json={'revision':row['revision']})
                assert response.status_code==200,response.text
                published = response.json()
                # Reopen storage to prove the index survives a new Repository instance.
                stored = Repository(tmp_path/'business.db').get('*','knowledge',identifier)
                assert stored['retrieval_index']['revision']==published['revision']
                assert chunks(stored)[0]['revision']==published['revision']
                result = (await client.post('/v2/admin/knowledge/search',json={'query':'芦苇渡口'})).json()
                assert result['items'][0]['document_id']==identifier and not result['external_called']
                assert 'vectors' not in (await client.get('/v2/admin/knowledge')).json()['items'][0]
                # Stale edits cannot overwrite a newer publication.
                assert (await client.put(path,json=body)).status_code==409
                body.update(revision=published['revision'],pieces=[{'location':'正文','text':'合成星河广场午后开放。'}])
                edited = (await client.put(path,json=body)).json()
                assert edited['status']=='draft' and 'retrieval_index' not in edited
                assert edited['original_filename']=='public.txt'
                assert (await client.post('/v2/admin/knowledge/search',json={'query':'芦苇渡口'})).json()['items']==[]
                published = (await client.post(path+'/publish',json={'revision':edited['revision']})).json()
                assert '星河广场' in (await client.post('/v2/admin/knowledge/search',json={'query':'星河广场'})).json()['items'][0]['text']
                assert (await client.post(path+'/withdraw',json={'revision':published['revision']})).status_code==200
                assert (await client.post('/v2/admin/knowledge/search',json={'query':'星河广场'})).json()['items']==[]
                assert (await client.get(path+'/original')).content==text.encode()  # original remains immutable
                assert (await client.delete(path)).status_code==200
                assert repo.knowledge_file(identifier) is None
                assert (await client.post('/v2/admin/knowledge/import',files=upload)).status_code==200
                assert (await client.post('/v2/admin/knowledge/import',files={'file':('x.txt',b'\xff')})).status_code==422
                await client.post('/auth/register',json={'username':'lifecycleuser','password':'test123456'})
                assert (await client.get('/v2/admin/knowledge')).status_code==403
                assert (await client.get(path+'/original')).status_code==403
        accounts.engine.dispose()
    asyncio.run(scenario())


class Embeddings:
    def __init__(self):
        self.fail = False
        self.bad_count = False

    async def embed(self, texts):
        if self.fail:
            raise RuntimeError('synthetic unavailable')
        return [] if self.bad_count else [[1.,0.] for _ in texts]


def test_vector_publication_semantic_search_and_fallback(tmp_path):
    async def scenario():
        repo = Repository(tmp_path/'business.db')
        accounts = Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('vectoradmin','test123456',role='admin')
        model = Embeddings()
        settings = Settings(mode='live',external_enabled=False,embedding_key='synthetic',embedding_model='test-model',dimensions=2)
        app = create_app(settings,repo,model,accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                await client.post('/auth/login',json={'username':'vectoradmin','password':'test123456'})
                body = {'title':'合成索引','pieces':[{'location':'第1页','text':'青松谷开放日。'}],
                        'url':'https://example.org/guide','institution':'合成机构','rights':'测试','published_at':'2026-10-19'}
                row = (await client.post('/v2/admin/knowledge',json=body)).json()
                path = '/v2/admin/knowledge/'+row['id']
                model.bad_count = True
                assert (await client.post(path+'/publish',json={'revision':1})).status_code==502
                assert repo.get('*','knowledge',row['id'])['status']=='draft'
                model.bad_count = False
                response = await client.post(path+'/publish',json={'revision':1})
                assert response.status_code==200,response.text
                stored = repo.get('*','knowledge',row['id'])
                assert chunks(stored)[0]['embedding']==[1.,0.]
                assert public_chunks(stored,'different-model')[0]['embedding'] is None
                # No lexical overlap: synthetic vectors deliberately encode equivalence.
                result = (await client.post('/v2/admin/knowledge/search',json={'query':'XYZ'})).json()
                assert result['method']=='hybrid' and result['items'][0]['document_id']==row['id']
                model.fail = True
                result = (await client.post('/v2/admin/knowledge/search',json={'query':'青松谷'})).json()
                assert result['method']=='bm25_fallback' and result['items']
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_public_question_uses_saved_vectors_and_no_external_when_sufficient(tmp_path,monkeypatch):
    async def scenario():
        import app.followup.public_rag as pipeline
        repo = Repository(tmp_path/'business.db')
        doc = repo.create('*','knowledge','',{'title':'合成文档','kind':'public','status':'published',
                          'pieces':[{'location':'第1页','text':'青松谷开放日。'}],
                          'embedding_model':'test-model','vectors':{'青松谷开放日。':[1.,0.]}})
        index = build_index({**doc,'revision':2})
        repo.update('*','knowledge',doc['id'],{**doc,'retrieval_index':index},1)
        async def rewrite(*args): return ['XYZ']
        async def review(*args):
            sources = args[-1]
            return SimpleNamespace(sufficient=True,source_ids=[s['id'] for s in sources],model_dump=lambda:{'sufficient':True})
        monkeypatch.setattr(pipeline,'rewrite_knowledge',rewrite)
        monkeypatch.setattr(pipeline,'review_evidence',review)
        settings = Settings(mode='live',external_enabled=True,embedding_key='synthetic',embedding_model='test-model')
        result = await public_sources(settings,repo,Embeddings(),'synthetic-run','XYZ',[])
        assert result['sources'][0]['document_id']==doc['id']
        assert result['external_status']=='not_needed'
    asyncio.run(scenario())
