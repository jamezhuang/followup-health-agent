import asyncio
import httpx
from app.config import Settings
from app.storage import Store
from app.v2_main import create_app
from app.followup.store import Repository
from app.followup.hospital import hospital_sources
from scripts.import_public_knowledge import body_text,pieces


def test_general_knowledge_and_dated_directory_can_be_published_without_fake_disease(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('kbadmin01','test123456',role='admin')
        app=create_app(Settings(mode='demo',external_enabled=False),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                await client.post('/auth/login',json={'username':'kbadmin01','password':'test123456'})
                body={'title':'合成医院公开名录','pieces':[{'location':'表1','text':'深圳：合成一医院、合成二医院、合成三医院、合成四医院。'}],
                      'url':'https://example.gov.cn/directory','institution':'合成发布机构','rights':'合成测试资料','diseases':[],
                      'regions':['广东省','深圳市'],'source_type':'directory_snapshot','collected_at':'2026-10-18'}
                row=(await client.post('/v2/admin/knowledge',json=body)).json()
                response=await client.post('/v2/admin/knowledge/'+row['id']+'/publish',json={'revision':row['revision']})
                assert response.status_code==200,response.text
                assert response.json()['published_at']=='' and response.json()['diseases']==[]
                result=await hospital_sources(Settings(mode='demo',external_enabled=False),repo,'run','广东省有哪些医院',return_details=True)
                assert result['local_source_count'] and result['directory_entry_count']==4
                # No source date may be invented for an undated ordinary article.
                invalid=(await client.post('/v2/admin/knowledge',json={**body,'source_type':'article','collected_at':''})).json()
                assert (await client.post('/v2/admin/knowledge/'+invalid['id']+'/publish',json={'revision':invalid['revision']})).status_code==422
                await client.post('/auth/register',json={'username':'kbuser01','password':'test123456'})
                assert (await client.post('/v2/admin/knowledge',json=body)).status_code==403
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_import_keeps_pdf_page_boundaries_and_removes_portal_footer():
    text='正文标题\n'+('健康知识。'*60)+'\n附件下载：\n福建省儿童医院\n网站地图'
    assert '福建省儿童医院' not in body_text(text,'正文标题')
    pages=pieces('[PDF第1页]\n'+('⼈体健康。'*30)+'\n[PDF第2页]\n'+('安全用药。'*30))
    assert len(pages)==2 and pages[0]['location'].startswith('PDF第1页')
    assert pages[1]['location'].startswith('PDF第2页') and '人体' in pages[0]['text']
