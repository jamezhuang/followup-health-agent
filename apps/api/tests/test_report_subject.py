import asyncio
import httpx
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.v2_main import create_app


def test_subject_is_proposed_from_report_and_created_only_after_confirmation(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app=create_app(Settings(mode='demo', recognition_provider='disabled'),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                await client.post('/auth/register',json={'username':'reportperson01','password':'test123456'})
                raw='姓名：合成患者\n性别：男 年龄：68岁\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：胸部CT平扫+增强'.encode()
                uploaded=await client.post('/v2/chat-attachments',files={'file':('synthetic.txt',raw)})
                assert uploaded.status_code==200,uploaded.text
                attachment=uploaded.json()
                proposal=(await client.get('/v2/chat-attachments/'+attachment['id']+'/subject-proposal')).json()
                assert proposal['name']=='合成患者' and proposal['sex']=='男' and proposal['age']=='68岁'
                assert proposal['relation']=='' and proposal['created_subject_id']==''
                assert (await client.get('/v2/subjects')).json()['items']==[]
                data={'name':'爸爸','patient_name':'合成患者','sex':'男','age':'68岁','relation':'父亲'}
                denied=await client.post('/v2/chat-attachments/'+attachment['id']+'/subject',json=data)
                assert denied.status_code==422
                assert (await client.get('/v2/subjects')).json()['items']==[]
                invalid=await client.post('/v2/chat-attachments/'+attachment['id']+'/subject',json={**data,'name':'  ','confirmed':True})
                assert invalid.status_code==422
                # Concurrent retry must not create two profiles from the same confirmation.
                responses=await asyncio.gather(*[client.post('/v2/chat-attachments/'+attachment['id']+'/subject',json={**data,'confirmed':True}) for _ in range(2)])
                assert all(r.status_code==200 for r in responses),[r.text for r in responses]
                person=responses[0].json()
                assert responses[1].json()['id']==person['id']
                assert len((await client.get('/v2/subjects')).json()['items'])==1
                assert person['name']=='爸爸' and person['patient_name']=='合成患者' and person['birth_date']==''
                assert person['diseases']==[] and person['allergies']==''
                assert person['source_attachment_id']==attachment['id']
                assert (await client.get('/v2/documents',params={'subject':person['id']})).json()['items']==[]
                proposal=(await client.get('/v2/chat-attachments/'+attachment['id']+'/subject-proposal')).json()
                assert proposal['created_subject_id']==person['id']
                draft=(await client.post('/v2/chat-attachments/'+attachment['id']+'/archive-review',params={'subject':person['id']})).json()
                saved=await client.post('/v2/report-reviews/'+draft['id']+'/confirm',json={'revision':draft['revision'],'confirmed':True})
                assert saved.status_code==200,saved.text
                assert saved.json()['subject']==person['id']
                unknown=(await client.post('/v2/chat-attachments',files={'file':('unknown.txt',b'Synthetic unknown fields')})).json()
                absent=(await client.get('/v2/chat-attachments/'+unknown['id']+'/subject-proposal')).json()
                assert absent['name']==absent['sex']==absent['age']==''
                assert len((await client.get('/v2/subjects')).json()['items'])==1
                await client.post('/auth/register',json={'username':'reportperson02','password':'test123456'})
                assert (await client.get('/v2/chat-attachments/'+attachment['id']+'/subject-proposal')).status_code==404
                assert (await client.post('/v2/chat-attachments/'+attachment['id']+'/subject',json={**data,'confirmed':True})).status_code==404
        accounts.engine.dispose()
    asyncio.run(scenario())
