import asyncio
import httpx
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.v2_main import create_app


def test_chat_upload_works_without_profile_and_archive_is_explicit_and_scoped(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app=create_app(Settings(mode='demo', recognition_provider='disabled'), repo, account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                await client.post('/auth/register',json={'username':'chatfiles01','password':'test123456'})
                raw='姓名：合成患者\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：胸部CT平扫+增强\n影像学意见：建议定期复查。'.encode()
                response=await client.post('/v2/chat-attachments',files={'file':('synthetic.txt',raw)})
                assert response.status_code==200,response.text
                attachment=response.json()
                conv=attachment['conversation']['id']
                assert 'data' not in attachment and not attachment['confirmed']
                assert (await client.get('/v2/subjects')).json()['items']==[]
                owner=(await client.get('/auth/me')).json()['user']['id']
                for kind in ('document','record','followup_info','todo','report_review','followup_upload'):
                    assert repo.items(owner,kind)==[],kind
                assert (await client.get('/v2/documents/'+attachment['id']+'/file')).content==raw
                restored=(await client.get('/v2/conversations/'+conv)).json()
                assert restored['pending_attachments'][0]['id']==attachment['id']
                assert 'data' not in restored['pending_attachments'][0]
                async def run(goal, conversation=conv, documents=None):
                    response=await client.post('/v2/runs',json={'goal':goal,'conversation_id':conversation,'document_ids':documents or []})
                    assert response.status_code==200,response.text
                    row=response.json()
                    for _ in range(250):
                        row=(await client.get('/v2/runs/'+row['id'])).json()
                        if row['status']!='running': break
                        await asyncio.sleep(.01)
                    assert row['status']=='completed',row
                    return row
                first=await run('我的检测报告检测项目是什么',documents=[attachment['id']])
                assert '胸部 CT 平扫' in first['result']['content'] and '自动识别' in first['result']['content']
                assert '已核对' not in first['result']['content']
                next_turn=await run('这份报告检测项目是什么')
                assert next_turn['result']['document_ids']==[attachment['id']]
                prepared=await run('给我复诊准备清单')
                assert prepared['result']['preparation_checklist'] and not prepared['result']['stale']
                compared=await run('对比这份和上次的报告')
                assert compared['result']['kind']=='chat' and compared['result']['operation']=='compare'
                saved=(await client.get('/v2/conversations/'+conv)).json()
                assert saved['pending_attachments']==[] and saved['messages'][0]['attachments'][0]['temporary']
                assert (await client.delete('/v2/chat-attachments/'+attachment['id'])).status_code==409
                unrelated=await run('我的检测报告检测项目是什么',conversation='')
                assert unrelated['result'].get('document_ids',[])==[]
                cross=await client.post('/v2/runs',json={'goal':'解读报告','conversation_id':unrelated['conversation_id'],'document_ids':[attachment['id']]})
                assert cross.status_code==404
                for kind in ('document','record','followup_info','todo','report_review'):
                    assert repo.items(owner,kind)==[],kind
                regen=await client.post('/v2/runs/'+next_turn['id']+'/regenerate')
                assert regen.status_code==200,regen.text
                regenerated=regen.json()
                for _ in range(250):
                    regenerated=(await client.get('/v2/runs/'+regenerated['id'])).json()
                    if regenerated['status']!='running': break
                    await asyncio.sleep(.01)
                assert regenerated['status']=='completed',regenerated
                clone=regenerated['result']['document_ids'][0]
                assert clone!=attachment['id']
                branch=(await client.get('/v2/conversations/'+regenerated['conversation_id'])).json()
                assert branch['messages'][0]['attachments'][0]['id']==clone
                person=(await client.post('/v2/subjects',json={'name':'合成患者','diseases':[]})).json()
                draft=(await client.post('/v2/chat-attachments/'+attachment['id']+'/archive-review',params={'subject':person['id']})).json()
                assert repo.items(owner,'document')==[]
                repeat=(await client.post('/v2/chat-attachments/'+attachment['id']+'/archive-review',params={'subject':person['id']})).json()
                assert repeat['id']==draft['id']
                edited={**draft['fields'], 'title':draft['title'], 'report_type':'inspection', 'revision':draft['revision']}
                edited['details']['examination_name']='腹部MR平扫'
                edited['report_date']='2026-07-08'
                updated_review=await client.put('/v2/report-reviews/'+draft['id'], json=edited)
                assert updated_review.status_code==200,updated_review.text
                draft=updated_review.json()
                archived=await client.post('/v2/report-reviews/'+draft['id']+'/confirm',json={'revision':draft['revision'],'confirmed':True})
                assert archived.status_code==200,archived.text
                document=archived.json()
                assert document['subject']==person['id'] and len(repo.items(owner,'document'))==1
                updated=(await client.get('/v2/conversations/'+conv)).json()
                assert updated['messages'][0]['attachments'][0]['archived']
                corrected=await run('这份报告检查项目是什么')
                assert '腹部 MR' in corrected['result']['content'] and '自动识别' not in corrected['result']['content']
                assert (await client.delete('/v2/conversations/'+conv)).status_code==200
                assert (await client.get('/v2/documents/'+attachment['id']+'/file')).status_code==404
                assert (await client.get('/v2/documents/'+document['id']+'/file')).content==raw
                assert (await client.get('/v2/documents/'+clone+'/file')).content==raw
                await client.post('/auth/register',json={'username':'chatfiles02','password':'test123456'})
                assert (await client.get('/v2/documents/'+clone+'/file')).status_code==404
                assert (await client.post('/v2/chat-attachments/'+clone+'/archive-review',params={'subject':person['id']})).status_code==404
        accounts.engine.dispose()
    asyncio.run(scenario())
