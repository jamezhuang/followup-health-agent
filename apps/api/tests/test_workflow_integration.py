import asyncio,json
import httpx
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.v2_main import create_app

KEY='synthetic-workflow-key-'+('x'*32)

def test_workflow_auth_scope_validation_and_result(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        settings=Settings(mode='demo',embedding_key='',external_enabled=False,workflow_api_key=KEY)
        app=create_app(settings,repo,account_store=accounts)
        root='/v2/integrations/workflow/tasks'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.post(root,json={'question':'你好'})).status_code==401
            headers={'X-CareBrief-Key':KEY}
            assert (await client.post(root,json={'question':'你好','subject':'private'},headers=headers)).status_code==422
            assert (await client.post(root,json={'question':'  '},headers=headers)).status_code==422
            assert (await client.post(root,json={'question':'a'*4001},headers=headers)).status_code==422
            response=await client.post(root,json={'question':'你好'},headers=headers)
            assert response.status_code==202,response.text
            task=response.json();identifier=task['id']
            assert task['status']=='running'
            assert repo.get('integration:workflow','run',identifier)
            for _ in range(200):
                await asyncio.sleep(.01)
                result=await client.get(root+'/'+identifier,headers=headers)
                if result.json()['status']!='running':break
            assert result.status_code==200 and result.json()['status']=='completed',result.text
            assert result.json()['result']
            assert not {'snapshot','conversation_id','document_ids','subject'} & result.json().keys()
            private=repo.create('patient-owner','run','private-subject',{'status':'completed','result':{'content':'PRIVATE'}})
            assert (await client.get(root+'/'+private['id'],headers=headers)).status_code==404
            assert (await client.get('/v2/runs/'+identifier,headers=headers)).status_code==401
            assert (await client.get('/v2/admin/dashboard',headers=headers)).status_code==401
            settings.workflow_client_id='other-platform'
            assert (await client.get(root+'/'+identifier,headers=headers)).status_code==404
            settings.workflow_api_key=''
            assert (await client.get(root+'/'+identifier,headers=headers)).status_code==404
        schema=app.openapi()
        operation=schema['paths'][root]['post']
        assert operation['security']==[{'CareBriefWorkflowKey':[]}]
        assert 'WorkflowTask' in json.dumps(operation['responses'])
    asyncio.run(scenario())

def test_workflow_reuses_running_task_guard_and_rotation(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        settings=Settings(mode='demo',embedding_key='',external_enabled=False,workflow_api_key=KEY)
        app=create_app(settings,repo,account_store=accounts)
        row=repo.create('integration:workflow','run','',{'status':'running','result':None})
        root='/v2/integrations/workflow/tasks'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={'X-CareBrief-Key':KEY}
            assert (await client.post(root,json={'question':'你好'},headers=headers)).status_code==409
            settings.workflow_api_key='rotated-'+KEY
            assert (await client.get(root+'/'+row['id'],headers=headers)).status_code==401
            assert (await client.get(root+'/'+row['id'],headers={'X-CareBrief-Key':settings.workflow_api_key})).status_code==200
    asyncio.run(scenario())

def test_export_contains_only_workflow_operations_and_no_private_schemas(tmp_path):
    from scripts.export_workflow_openapi import workflow_schema
    repo=Repository(tmp_path/'business.db')
    accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
    schema=create_app(Settings(mode='demo',workflow_api_key=KEY),repo,account_store=accounts).openapi()
    exported=workflow_schema(schema,'https://carebrief.example.invalid')
    assert len(exported['paths'])==2
    assert set(exported['components']['schemas'])=={'WorkflowQuestion','WorkflowTask','HTTPValidationError','ValidationError'}
    assert KEY not in json.dumps(exported)
    assert exported['servers'][0]['url']=='https://carebrief.example.invalid'
