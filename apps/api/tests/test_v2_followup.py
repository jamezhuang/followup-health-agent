import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository, Conflict
from app.followup.retrieval import chunks,hybrid_search
from app.followup.observability import ObservedClient,calling
from app.followup.workflow import Workflow,Plan,Review,Draft,Claim,Verification,compare_records
from app.v2_main import create_app


def test_scoped_search_version_and_optimistic_updates(tmp_path):
    repo=Repository(tmp_path/'test.db')
    row=repo.create('alice','document','father',{'title':'报告','pieces':[{'location':'第1页','text':'空腹血糖检查记录'}]})
    assert repo.get('bob','document',row['id']) is None
    assert repo.items('alice','document','mother')==[]
    revised=repo.update('alice','document',row['id'],{'title':'新报告','pieces':[{'location':'第2页','text':'糖化血红蛋白检查记录'}]},1)
    with pytest.raises(Conflict):
        repo.update('alice','document',row['id'],{},1)
    assert chunks(revised)[0]['revision']==2
    found=hybrid_search('糖化血红蛋白',chunks(revised))
    assert len(found)==1 and found[0]['location']=='第2页'
    assert hybrid_search('肺结节',chunks(revised))==[]


def test_numeric_comparison_uses_decimal_and_does_not_mix_units_or_conflicting_dates():
    rows=[{'id':'a','kind':'observation','label':'血糖','date':'2026-09-01','value':'6.0','unit':'mmol/L'},
          {'id':'b','kind':'observation','label':'血糖','date':'2026-10-01','value':'6.2','unit':'mmol/L'}]
    assert compare_records(rows)[0]['delta']=='0.2'
    assert compare_records([{**rows[0],'unit':'mg/dL'},rows[1]])==[]
    assert compare_records([{**rows[0],'unit':''},{**rows[1],'unit':''}])==[]
    assert compare_records(rows+[{**rows[1],'id':'c','value':'8.1'}])==[]


def test_expanded_context_always_contains_the_hit_even_late_in_a_long_page():
    rows=chunks({'id':'doc','revision':1,'title':'long page',
                 'pieces':[{'location':'page1','text':'早期正文。'*1500+'末尾检查解释。'*600}]})
    assert len(rows)>10
    assert all(row['text'] in row['parent'] for row in rows)


def test_every_http_attempt_logs_usage_or_unknown_without_secrets(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'calls.db')
        requests=[]
        async def provider(request):
            requests.append(request)
            if len(requests)==1:
                return httpx.Response(429,json={'error':'private-secret-patient'})
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'done'}}],
                    'usage':{'prompt_tokens':11,'completion_tokens':3,'total_tokens':14}})
        settings=Settings(mode='live',deepseek_key='sk-private-key',deepseek_base='https://example.test')
        client=ObservedClient(settings,repo,httpx.MockTransport(provider))
        with calling('run-a','医学证据 Agent','retrieval_critique'):
            await client.complete([{'role':'user','content':'private-patient-text'}])
        rows=repo.calls()
        assert len(rows)==2
        assert sorted(x['attempt'] for x in rows)==[1,2]
        failed=next(x for x in rows if x['status']=='failed')
        assert failed['total_tokens'] is None
        assert failed['error_code']=='rate_limited'
        assert next(x for x in rows if x['status']=='ok')['total_tokens']==14
        raw=json.dumps(rows)
        assert all(secret not in raw for secret in ['sk-private-key','private-patient-text','private-secret-patient'])
    asyncio.run(scenario())


def test_timeout_cancellation_and_restart_are_observable(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'cancel.db')
        entered=asyncio.Event()
        async def provider(request):
            entered.set()
            await asyncio.sleep(10)
        client=ObservedClient(Settings(mode='live',deepseek_key='test'),repo,httpx.MockTransport(provider))
        task=asyncio.create_task(client.complete([{'role':'user','content':'test'}]))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert repo.calls()[0]['status']=='cancelled'
        repo.begin_call('abandoned','worker','background','model',1)
        repo.recover()
        assert next(c for c in repo.calls() if c['run_id']=='abandoned')['status']=='interrupted'
    asyncio.run(scenario())


def test_reflection_requeries_and_child_contexts_are_isolated(tmp_path):
    class Model:
        settings=SimpleNamespace(embedding_key='')
        def __init__(self):
            self.requests=[]
            self.reviews=0
        async def structured(self,schema,instruction,data):
            self.requests.append((schema.__name__,data))
            assert 'messages' not in data
            if schema is Plan:
                assert 'chunks' not in data and 'records' not in data
                return Plan(queries=['空腹血糖'],focus='比较记录')
            if schema is Review:
                self.reviews+=1
                if self.reviews==1:
                    return Review(action='refine',next_query='糖化血红蛋白',gaps=['补查第二个项目'])
                return Review(action='generate',relevant_ids=[data['sources'][0]['id']])
            if schema is Draft:
                x=data['evidence'][0]
                return Draft(claims=[Claim(text=x['text'],quote=x['text'],evidence_id=x['id']),
                                     Claim(text='编造诊断',quote='不存在的引文',evidence_id=x['id'])],questions=[])
            return Verification(supported_indices=[0])
    async def scenario():
        repo=Repository(tmp_path/'workflow.db')
        model=Model()
        source={'id':'doc','revision':1,'title':'测试资料','pieces':[{'location':'第1页','text':'空腹血糖记录；糖化血红蛋白记录。'}]}
        packet={'diseases':['diabetes'],'range':'两次','constraints':'不调整用药','records':[],
                'chunks':chunks(source),'previous_result':None}
        result=await Workflow(repo,model,True).run('r','整理复诊',packet)
        assert result['iterations']==2
        assert result['rejected_claims']==1
        assert len(result['claims'])==1
        assert any(x['action']=='按证据缺口重新委派' for x in repo.trace('r')['stages'])
        assert not repo.calls()  # Test doubles are not represented as real model calls.
    asyncio.run(scenario())


def test_reflection_has_a_hard_budget(tmp_path):
    class Model:
        settings=SimpleNamespace(embedding_key='')
        async def structured(self,schema,instruction,data):
            if schema is Plan:return Plan(queries=['血糖'],focus='整理')
            if schema is Review:return Review(action='refine',next_query='全新查询',gaps=['资料缺失'])
            if schema is Draft:return Draft()
            return Verification()
    async def scenario():
        packet={'diseases':['diabetes'],'range':'','constraints':'','records':[],'chunks':[]}
        result=await Workflow(Repository(tmp_path/'bounded.db'),Model(),True).run('r','复诊',packet)
        assert result['iterations']<=3
        assert result['stop_reason'] in {'no_new_evidence','repeated_query','iteration_budget'}
    asyncio.run(scenario())


def test_full_api_workflow_crud_roles_and_source_publication(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'v2.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('admin001','admin123',role='admin')
        app=create_app(Settings(mode='demo'),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            transport=httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,base_url='http://test') as user, httpx.AsyncClient(transport=transport,base_url='http://test') as admin, httpx.AsyncClient(transport=transport,base_url='http://test') as stranger:
                assert (await user.get('/v2/admin/dashboard')).status_code==401
                assert (await user.post('/auth/register',json={'username':'123456','password':'abcdef'})).status_code==200
                assert (await user.get('/v2/admin/dashboard')).status_code==403
                assert (await user.get('/v2/admin/traces/anything')).status_code==403
                await admin.post('/auth/login',json={'username':'admin001','password':'admin123'})
                await stranger.post('/auth/register',json={'username':'other001','password':'123456'})
                subject=(await user.post('/v2/subjects',json={'name':'测试对象','diseases':['diabetes'],'constraints':'不要改药'})).json()
                sid=subject['id']
                assert (await stranger.get('/v2/records',params={'subject':sid})).status_code==404
                assert (await user.post('/v2/subjects',json={'name':'不支持','diseases':['unknown']})).status_code==422
                for day,value in [('2026-09-01','6.0'),('2026-10-01','6.2')]:
                    assert (await user.post('/v2/records',json={'subject':sid,'date':day,'kind':'observation','label':'空腹血糖','value':value,'unit':'mmol/L'})).status_code==200
                private=(await user.post('/v2/documents',json={'subject':sid,'title':'待确认报告','pieces':[{'location':'第1页','text':'SHOULD_NOT_BE_RETRIEVED'}]})).json()
                assert (await user.post('/v2/runs',json={'subject':sid,'goal':'准备复诊','document_ids':[private['id']]})).status_code==422
                document={'title':'测试专业资料','pieces':[{'location':'第一节','text':'2型糖尿病复诊记录，核对检查资料与日期。'}],
                          'diseases':['diabetes'],'institution':'测试机构','url':'https://example.org/source','published_at':'2026-01-01','rights':'仅用于合成测试'}
                assert (await user.post('/v2/admin/knowledge',json=document)).status_code==403
                public=(await admin.post('/v2/admin/knowledge',json=document)).json()
                assert public['status']=='draft'
                response=await admin.post('/v2/admin/knowledge/'+public['id']+'/publish',json={'revision':1})
                assert response.status_code==200 and response.json()['status']=='published'
                row=(await user.post('/v2/runs',json={'subject':sid,'goal':'准备复诊'})).json()
                rid=row['id']
                assert (await stranger.get('/v2/runs/'+rid)).status_code==404
                for _ in range(100):
                    await asyncio.sleep(.01)
                    status=(await user.get('/v2/runs/'+rid)).json()
                    if status['status']!='running':break
                assert status['status']=='completed',status
                assert status['result']['mode']=='demo'
                assert len(status['result']['records'])==2
                assert status['result']['comparisons'][0]['delta']=='0.2'
                assert status['result']['stale'] is False
                assert status['result']['search_queries']
                assert any(source['document_id']==public['id'] for source in status['result']['search_sources'])
                assert all('embedding' not in source and 'parent' not in source for source in status['result']['search_sources'])
                assert 'SHOULD_NOT_BE_RETRIEVED' not in json.dumps(status)
                dashboard=(await admin.get('/v2/admin/dashboard')).json()
                assert dashboard['metrics']['calls']==0
                trace=(await admin.get('/v2/admin/traces/'+rid)).json()
                assert len(trace['stages'])>=7
                conv=(await user.get('/v2/conversations/'+row['conversation_id'])).json()
                assert len(conv['messages'])==2
                record=(await user.get('/v2/records',params={'subject':sid})).json()['items'][0]
                revised={k:v for k,v in record.items() if k in {'subject','date','kind','label','value','unit','revision'}}
                revised['value']='7.0'
                assert (await user.put('/v2/records/'+record['id'],json=revised)).status_code==200
                assert (await user.get('/v2/runs/'+rid)).json()['result']['stale'] is True
                assert (await user.delete('/v2/documents/'+private['id'])).status_code==200
                assert (await user.delete('/v2/subjects/'+sid)).status_code==200
                assert (await user.get('/v2/runs/'+rid)).status_code==404
        accounts.engine.dispose()
    asyncio.run(scenario())
