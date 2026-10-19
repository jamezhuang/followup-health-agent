import asyncio
import httpx
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.intent import IntentDecision,analyze_intent
from app.v2_main import create_app


def test_two_branches_with_public_tools_private_scope_and_clarification(tmp_path):
    class Client:
        def __init__(self):self.answers=[]
        async def structured(self,schema,instruction,data):
            assert schema is IntentDecision
            message=data['message']
            if message=='这个怎么办':
                return IntentDecision(intent='followup',needs_clarification=True,reason_code='ambiguous')
            if '抗生素' in message:
                return IntentDecision(intent='daily',consultation_type='medical_education',reason_code='general_medical')
            if '复诊' in message:
                return IntentDecision(intent='followup',operation='prepare',reason_code='personal_followup')
            return IntentDecision(intent='daily',reason_code='general_task')
        async def complete(self,messages,**kwargs):
            self.answers.append(messages)
            return {'content':'测试回复'}

    async def scenario():
        repo=Repository(tmp_path/'v2.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        client=Client()
        app=create_app(Settings(mode='live',external_enabled=False),repo,client=client,account_store=accounts)
        graph_calls=[]
        async def graph(run_id,goal,packet):
            graph_calls.append(packet)
            return {'claims':[],'records':[],'sources':[],'gaps':[],'questions':[],
                    'notice':'','mode':'live','rejected_claims':0,'iterations':1,'stop_reason':'test'}
        app.state.workflow.run=graph
        repo.create('*','knowledge','',{'title':'公共科普测试','status':'published','diseases':['hypertension'],
                    'pieces':[{'location':'第一节','text':'抗生素知识科普：PUBLIC_KNOWLEDGE_ONLY'}]})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
                await api.post('/auth/register',json={'username':'intent01','password':'123456'})
                sid=(await api.post('/v2/subjects',json={'name':'自己','diseases':['hypertension']})).json()['id']
                await api.post('/v2/documents',json={'subject':sid,'title':'私密报告','confirmed':True,
                    'pieces':[{'location':'正文','text':'PRIVATE_REPORT_ONLY'}]})
                async def turn(message,subject=''):
                    row=(await api.post('/v2/runs',json={'subject':subject,'goal':message})).json()
                    for _ in range(100):
                        await asyncio.sleep(.005)
                        row=(await api.get('/v2/runs/'+row['id'])).json()
                        if row['status']!='running':break
                    assert row['status']=='completed',row
                    return row['result']
                result=await turn('帮我安排早晨的日常事务',sid)
                assert result['intent']=='daily'
                assert 'PRIVATE_REPORT_ONLY' not in str(client.answers[-1])
                result=await turn('抗生素是什么')
                assert result['intent']=='daily' and result['consultation_type']=='medical_education' and result['sources']
                assert 'PUBLIC_KNOWLEDGE_ONLY' in str(client.answers[-1])
                assert 'PRIVATE_REPORT_ONLY' not in str(client.answers[-1])
                result=await turn('帮我整理复诊资料',sid)
                assert result['intent']=='followup' and len(graph_calls)==1
                assert any('PRIVATE_REPORT_ONLY' in c['text'] for c in graph_calls[0]['chunks'])
                result=await turn('帮我准备复诊')
                assert result['intent']=='followup' and result['operation']=='prepare'
                result=await turn('这个怎么办',sid)
                assert result['stop_reason']=='needs_clarification'
                assert len(graph_calls)==1
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_classifier_failure_has_safe_local_fallback(tmp_path):
    class Broken:
        async def structured(self,*args,**kwargs):raise RuntimeError('secret-provider-error')
    async def scenario():
        repo=Repository(tmp_path/'fallback.db')
        result=await analyze_intent(repo,Broken(),True,'r','抗生素是什么',[],False)
        assert result.intent=='daily' and result.consultation_type=='medical_education'
        trace=repo.trace('r')
        assert 'secret-provider-error' not in str(trace)
        assert any(x.get('reason')=='classifier_unavailable' for x in trace['stages'])
    asyncio.run(scenario())
