"""Public hospital tasks must coexist with temporary report conversations."""
import asyncio
from types import SimpleNamespace
import httpx
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.v2_main import create_app


def test_report_upload_hospital_followups_and_return_to_report(tmp_path, monkeypatch):
    from app import web_tools
    searches=[]
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['bocha'])
            self.browser=SimpleNamespace(enabled=False)
        async def search(self,args):
            searches.append(args['query'])
            return {'ok':True,'candidates':[{'candidate_id':'public','title':'示例医院 广东省 深圳市 医院名录','snippet':'地址 重点科室'}]}
        async def read(self,args):
            return {'ok':True,'sources':[{'id':'public-source','title':'医院服务介绍',
                'text':'广东省示例医院位于深圳市，地址：示例路1号。重点专科为呼吸内科。',
                'url':'https://example.com/hospital'}]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app=create_app(Settings(mode='demo',external_enabled=True,recognition_provider='disabled'),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                await client.post('/auth/register',json={'username':'hospitalroute01','password':'test123456'})
                attachment=(await client.post('/v2/chat-attachments',files={'file':('synthetic.txt',
                    '姓名：合成患者\n检查项目：胸部CT平扫+增强\n检查日期：2026-07-06'.encode())})).json()
                async def run(goal,ids=None):
                    response=await client.post('/v2/runs',json={'goal':goal,'conversation_id':attachment['conversation_id'],'document_ids':ids or []})
                    assert response.status_code==200,response.text
                    row=response.json()
                    for _ in range(300):
                        row=(await client.get('/v2/runs/'+row['id'])).json()
                        if row['status']!='running':break
                        await asyncio.sleep(.01)
                    assert row['status']=='completed',row
                    return row['result']
                first=await run('广东省有那些医院',[attachment['id']])
                assert first['intent']=='daily' and first['consultation_type']=='hospital' and first['sources']
                assert first['hospital_context']['kind']=='directory'
                assert first['attachment_context_ids']==[attachment['id']]
                second=await run('示例医院的地址在哪')
                assert second['hospital_context']['names']==['示例医院']
                third=await run('重点科室呢')
                assert third['intent']=='daily' and third['consultation_type']=='hospital' and third['hospital_context']['names']==['示例医院']
                assert 'specialty' in third['hospital_context']['topics']
                city=await run('深圳有哪些医院')
                assert city['hospital_context']['kind']=='directory' and city['hospital_context']['region']=='深圳'
                report=await run('这份报告检查项目是什么')
                assert report['intent']=='followup' and '胸部 CT' in report['content']
                assert report['document_ids']==[attachment['id']]
                assert all('合成患者' not in query and '2026' not in query for query in searches)
                assert (await client.get('/v2/subjects')).json()['items']==[]
        accounts.engine.dispose()
    asyncio.run(scenario())
