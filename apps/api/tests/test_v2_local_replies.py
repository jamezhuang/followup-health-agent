import asyncio
import httpx
import pytest
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.local_replies import local_reply
from app.v2_main import create_app


@pytest.mark.parametrize('text',[
    '你好，我胸痛怎么办','你好，你能帮我对比两次报告吗','谢谢，但我想问抗生素是什么',
    '你好你能做什么？顺便记住我喜欢蓝色','继续','好','这些呢','帮我写一篇文章',
])
def test_mixed_or_contextual_messages_never_get_swallowed_by_local_templates(text):
    assert local_reply(text) is None


def test_fixed_questions_skip_both_classifier_and_answer_provider_calls(tmp_path):
    class NoModel:
        def __init__(self):self.invocations=0
        async def structured(self,*args,**kwargs):
            self.invocations+=1
            raise AssertionError('fixed question must not call classifier')
        async def complete(self,*args,**kwargs):
            self.invocations+=1
            raise AssertionError('fixed question must not call answer model')
    async def scenario():
        repo=Repository(tmp_path/'v2.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        model=NoModel()
        app=create_app(Settings(mode='live'),repo,client=model,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
                await api.post('/auth/register',json={'username':'local001','password':'123456'})
                for question in ['你好！','你好，你能做什么？','怎么上传报告','怎么创建健康档案','怎么收起侧边栏','谢谢你','再见']:
                    row=(await api.post('/v2/runs',json={'goal':question})).json()
                    for _ in range(100):
                        await asyncio.sleep(.005)
                        row=(await api.get('/v2/runs/'+row['id'])).json()
                        if row['status']!='running':break
                    assert row['status']=='completed',row
                    assert row['mode']=='local'
                    assert row['result']['response_method']=='local_rule'
                    assert row['result']['intent']=='daily'
                    assert row['result']['content']
                    assert any(stage['agent']=='本地回复' for stage in repo.trace(row['id'])['stages'])
                assert model.invocations==0
                assert repo.calls()==[]
        accounts.engine.dispose()
    asyncio.run(scenario())
