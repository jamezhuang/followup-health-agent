import asyncio
from types import SimpleNamespace
import httpx
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.chat import needs_followup
from app.v2_main import create_app


def test_greetings_and_general_questions_are_not_forced_into_followup():
    prior=[{'role':'assistant','result':{'claims':[]}}]
    for text in ['你好你能做什么','你好','你是谁','复诊是什么意思','帮我写一首诗']:
        assert needs_followup(text,prior) is False
    for text in ['你好，请帮我准备复诊资料','对比两次检查报告','继续整理','帮我看看报告']:
        assert needs_followup(text,prior) is True
    assert needs_followup('继续聊聊',[{'role':'assistant','result':{'kind':'chat'}}]) is False


def test_chat_without_subject_has_recent_history_and_never_reads_private_documents(tmp_path):
    class Client:
        settings=SimpleNamespace(embedding_key='')
        def __init__(self):self.inputs=[]
        async def complete(self,messages,**kwargs):
            self.inputs.append(messages)
            return {'content':'你好！可以直接聊天，也可以解释健康知识。'}

    async def scenario():
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        repo=Repository(tmp_path/'v2.db')
        model=Client()
        app=create_app(Settings(mode='live'),repo,client=model,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                await client.post('/auth/register',json={'username':'chatuser','password':'123456'})
                async def turn(goal,conversation='',subject=''):
                    response=await client.post('/v2/runs',json={'goal':goal,'conversation_id':conversation,'subject':subject})
                    assert response.status_code==200,response.text
                    row=response.json()
                    for _ in range(100):
                        await asyncio.sleep(.005)
                        row=(await client.get('/v2/runs/'+row['id'])).json()
                        if row['status']!='running':break
                    assert row['status']=='completed'
                    assert row['result']['kind']==('followup' if '准备复诊' in goal else 'chat')
                    assert row['result']['stale'] is False
                    return row
                first=await turn('你好你能做什么，我叫测试昵称')
                await turn('我刚才说我叫什么？',first['conversation_id'])
                assert any('测试昵称' in m['content'] for m in model.inputs[-1][1:-1])
                subject=(await client.post('/v2/subjects',json={'name':'自己','diseases':['hypertension']})).json()['id']
                await client.post('/v2/documents',json={'subject':subject,'title':'私密报告','confirmed':True,
                    'pieces':[{'location':'正文','text':'SECRET_PRIVATE_RECORD'}]})
                await turn('你好',subject=subject)
                assert 'SECRET_PRIVATE_RECORD' not in str(model.inputs)
                assert len(model.inputs)==2  # The final plain greeting is handled locally.
                conv=(await client.get('/v2/conversations/'+first['conversation_id'])).json()
                assert len(conv['messages'])==4
                # A missing profile is an invitation to clarify, not a disabled chat input.
                await turn('帮我准备复诊资料')
        accounts.engine.dispose()
    asyncio.run(scenario())
