import asyncio
import base64
import json
import httpx
import pytest
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.intent import fallback_intent, requested_operation
from app.followup.preparation import preparation_requested
from app.followup.report_dialogue import answer_report
from app.followup.recognition import Recognition
from app.v2_main import create_app


@pytest.mark.parametrize('goal,operation', [
    ('我的检测报告检测项目是什么', 'answer'),
    ('解读这份报告', 'answer'), ('报告说需要复诊是什么意思', 'answer'),
    ('多久复诊', 'answer'), ('不要复诊清单，只分析结果', 'answer'),
    ('请给我复诊需要准备的东西清单', 'prepare'),
    ('比较两次报告', 'compare'),
    ('两次血压单位不同，先看看能不能对比', 'answer'),
    ('我的报告缺了单位，还能直接比较吗', 'answer'),
    ('请换算单位并比较两次报告', 'compare'),
])
def test_current_question_controls_operation(goal, operation):
    assert requested_operation(goal) == operation
    assert preparation_requested(goal, {'operation': 'prepare'}) == (operation == 'prepare')


def test_report_upload_multiturn_and_original_files_are_scoped_and_persisted(tmp_path, monkeypatch):
    parse_original = Recognition.parse_document
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII=')
    async def synthetic_recognition(self, data, suffix, operation):
        if data == png:
            return [{'location':'第1页','text':'姓名：合成患者\n检查项目：胸部CT平扫'}]
        return await parse_original(self, data, suffix, operation)
    monkeypatch.setattr(Recognition, 'parse_document', synthetic_recognition)
    async def scenario():
        repo = Repository(tmp_path/'business.db')
        accounts = Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app = create_app(Settings(mode='demo', recognition_provider='disabled'), repo, account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                await client.post('/auth/register', json={'username':'dialogue01','password':'test123456'})
                person = (await client.post('/v2/subjects', json={'name':'合成患者','diseases':[]})).json()
                ids = []
                contents = []
                for project in ['胸部CT平扫+增强', '盆腔MR平扫']:
                    raw = ('姓名：合成患者\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：'+project+'\n影像学表现：合成描述。').encode()
                    contents.append(raw)
                    draft = (await client.post('/v2/report-reviews',params={'subject':person['id']},files={'file':(project+'.txt',raw)})).json()
                    doc = (await client.post('/v2/report-reviews/'+draft['id']+'/confirm',json={'revision':draft['revision'],'confirmed':True})).json()
                    ids.append(doc['id'])
                conversation = ''
                async def run(goal, documents=None):
                    nonlocal conversation
                    response = await client.post('/v2/runs',json={'subject':person['id'],'conversation_id':conversation,
                                                                 'goal':goal,'document_ids':documents or []})
                    assert response.status_code==200, response.text
                    row=response.json()
                    for _ in range(250):
                        row=(await client.get('/v2/runs/'+row['id'])).json()
                        if row['status']!='running': break
                        await asyncio.sleep(.01)
                    assert row['status']=='completed',row
                    conversation=row['conversation_id']
                    return row['result']
                first=await run('解读这份报告',[ids[0]])
                assert first['kind']=='chat' and 'preparation_checklist' not in first
                second=await run('我的检测报告检测项目是什么')
                assert second['operation']=='answer' and '胸部 CT 平扫' in second['content']
                assert 'MR' not in second['content'] and len(second['content']) < 180
                assert second['questions']==[] and second['document_ids']==[ids[0]]
                prepared=await run('给我复诊需要准备的东西清单')
                assert prepared['preparation_checklist']
                again=await run('这个检查项目是什么')
                assert again['kind']=='chat' and 'preparation_checklist' not in again
                new=await run('我的检测报告检测项目是什么',[ids[1]])
                assert 'MR' in new['content'] and 'CT' not in new['content']
                continued=await run('这份报告检查项目是什么')
                assert continued['document_ids']==[ids[1]]
                generic=await run('抗生素是什么')
                assert generic['intent']=='daily' and generic['consultation_type']=='medical_education' and generic['sources']==[]
                saved=(await client.get('/v2/conversations/'+conversation)).json()
                assert saved['messages'][0]['attachments'][0]['id']==ids[0]
                assert saved['messages'][2]['attachments']==[]
                original=await client.get('/v2/documents/'+ids[0]+'/file')
                assert original.content==contents[0]
                assert 'no-store' in original.headers['cache-control']
                image_review=(await client.post('/v2/report-reviews', params={'subject':person['id']},
                                               files={'file':('synthetic.png',png,'image/png')})).json()
                image_doc=(await client.post('/v2/report-reviews/'+image_review['id']+'/confirm',
                                             json={'revision':image_review['revision'],'confirmed':True})).json()
                image_file=await client.get('/v2/documents/'+image_doc['id']+'/file')
                assert image_file.content==png and image_file.headers['content-type']=='image/png'
                assert image_file.headers['content-disposition'].startswith('inline;')
                await client.post('/auth/register',json={'username':'dialogue02','password':'test123456'})
                assert (await client.get('/v2/documents/'+ids[0]+'/file')).status_code==404
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_live_report_answer_has_current_evidence_and_dialogue_without_preparation_template():
    class Client:
        async def complete(self, messages):
            self.messages=messages
            return {'content':'报告中的这个术语表示一项影像描述。'}
    async def scenario():
        client=Client()
        docs=[{'id':'one','revision':1,'subject':'person','title':'合成报告','confirmed':True,
               'pieces':[{'location':'第1页','text':'合成影像描述。'}],
               'key_fields':{'examination_date':'2026-07-06','report_date':'2026-07-07','details':{'examination_name':'胸部CT平扫'}}}]
        messages=[{'role':'user','content':'分析这份报告'},
                  {'role':'assistant','result':{'kind':'chat','intent':'followup','content':'上轮报告解读','document_ids':['one']}}]
        result=await answer_report(None,client,True,'test','这个术语是什么意思',messages,docs)
        assert result['kind']=='chat' and result['content']=='报告中的这个术语表示一项影像描述。'
        assert client.messages[-1]['content']=='这个术语是什么意思'
        assert client.messages[-2]['content']=='上轮报告解读'
        assert '2026-07-06' in client.messages[0]['content']
        assert '绝不自动转成复诊准备' in client.messages[0]['content']
        assert result['sources'][0]['document_id']=='one'
        assert fallback_intent('这个是什么意思',messages).intent=='followup'
        assert fallback_intent('CT是什么',messages).response_mode=='medical_education'
        messages.append({'role':'assistant','result':{'kind':'chat','intent':'medical_education','content':'一般知识'}})
        assert fallback_intent('这个是什么意思',messages).intent!='followup'
    asyncio.run(scenario())
