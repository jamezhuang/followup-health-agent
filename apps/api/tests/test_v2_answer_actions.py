import asyncio
from types import SimpleNamespace
import httpx
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.intent import IntentDecision
from app.v2_main import create_app


def test_answer_feedback_metadata_isolation_and_regeneration_context(tmp_path):
    class Client:
        settings=SimpleNamespace(embedding_key='')
        def __init__(self):self.inputs=[]
        async def structured(self,*args,**kwargs):
            return IntentDecision(intent='daily',reason_code='general_task')
        async def complete(self,messages,**kwargs):
            self.inputs.append(messages)
            return {'content':f'response-{len(self.inputs)}'}
    async def scenario():
        repo=Repository(tmp_path/'v2.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix());model=Client()
        app=create_app(Settings(mode='live'),repo,model,accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as other:
                await client.post('/auth/register',json={'username':'answer01','password':'answer01'})
                await other.post('/auth/register',json={'username':'answer02','password':'answer02'})
                async def completed(response):
                    assert response.status_code==200,response.text
                    row=response.json()
                    for _ in range(100):
                        await asyncio.sleep(.005)
                        row=(await client.get('/v2/runs/'+row['id'])).json()
                        if row['status']!='running':break
                    assert row['status']=='completed',row
                    return row
                first=await completed(await client.post('/v2/runs',json={'goal':'first-request'}))
                cid=first['conversation_id']
                second=await completed(await client.post('/v2/runs',json={'goal':'second-request','conversation_id':cid}))
                await completed(await client.post('/v2/runs',json={'goal':'future-private-request','conversation_id':cid}))
                path='/v2/runs/'+second['id']
                assert (await other.put(path+'/feedback',json={'vote':'like'})).status_code==404
                assert (await other.post(path+'/regenerate')).status_code==404
                assert (await client.put(path+'/feedback',json={'vote':'invalid'})).status_code==422
                assert (await client.put(path+'/feedback',json={'vote':'like'})).json()['vote']=='like'
                conversation=(await client.get('/v2/conversations/'+cid)).json()
                answer=next(m for m in conversation['messages'] if m.get('run_id')==second['id'])
                assert answer['feedback']=='like' and isinstance(answer['duration_ms'],int)
                assert (await client.put(path+'/feedback',json={'vote':'dislike'})).json()['vote']=='dislike'
                assert (await client.put(path+'/feedback',json={'vote':None})).json()['vote'] is None
                regenerated=await completed(await client.post(path+'/regenerate'))
                assert regenerated['id']!=second['id'] and regenerated['conversation_id']!=cid
                context=model.inputs[-1][1:]
                assert [m['content'] for m in context]==['first-request','response-1','second-request']
                original=(await client.get('/v2/conversations/'+cid)).json()
                branch=(await client.get('/v2/conversations/'+regenerated['conversation_id'])).json()
                assert len(original['messages'])==6 and original['messages'][3]['result']['content']=='response-2'
                assert len(branch['messages'])==4 and branch['messages'][-1]['result']['content']=='response-4'
                # Feedback does not modify the medical result or become model context.
                assert all('feedback' not in m for m in model.inputs[-1])
                assert 'future-private-request' not in str(model.inputs[-1])
        accounts.engine.dispose()
    asyncio.run(scenario())
