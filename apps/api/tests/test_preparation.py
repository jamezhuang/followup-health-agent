import asyncio
import json
import httpx

from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.preparation import preparation_context, build_preparation, focused_questions, preparation_requested
from app.followup.memory import message_text
from app.v2_main import create_app
from app.followup.preparation import patient_preparation_notes, disease_preparation_suggestions


def test_patient_notes_keep_current_and_historical_provenance_and_disease_reminders():
    notes = patient_preparation_notes({'constraints': '想问头晕是否和用药有关'},
        {'summary': '希望医生解释复查结果'}, [
            {'confirmed': True, 'report_type': 'followup', 'summary': '以前提过夜间咳嗽', 'followup_date': '2026-06-01'},
            {'confirmed': False, 'report_type': 'followup', 'summary': '未核对内容'}])
    assert len(notes['items']) == 3
    assert '头晕' in notes['items'][0] and '历史信息' in notes['items'][2]
    assert '未核对内容' not in str(notes)
    assert '高血压' in disease_preparation_suggestions(['hypertension'])[0]
    assert disease_preparation_suggestions([]) == []


def test_report_photo_does_not_mean_images_ready_and_dates_are_clinical():
    docs = [{'id': 'ct', 'title': '合成CT报告.jpg', 'confirmed': True, 'created_at': '2026-10-18',
             'key_fields': {'examination_date': '2026-07-06', 'report_date': '2026-07-07',
                            'details': {'examination_name': '胸部 CT 平扫＋增强', 'examination_modality': 'CT', 'findings': '合成术后描述', 'impression': '建议定期复查'},
                            'medications': []}},
            {'id': 'pending', 'title': '未核对报告', 'confirmed': False}]
    context = preparation_context(docs, [])
    assert [row['id'] for row in context['reports']] == ['ct']
    result = build_preparation(context)
    by_id = {row['id']: row for row in result['checklist']}
    assert by_id['reports']['status'] == 'provided'
    assert '2026-07-06' in by_id['reports']['action']
    assert '2026-07-07' not in by_id['reports']['action'] and '2026-10-18' not in by_id['reports']['action']
    assert by_id['images']['status'] == 'to_confirm' and by_id['images']['document_ids'] == ['ct']
    assert '如有' in by_id['surgery_history']['action']
    assert '如未用药' in by_id['medications']['action']
    assert by_id['report_advice']['status'] == 'reminder'
    assert '具体时间' in by_id['report_advice']['action'] and '医生' in by_id['report_advice']['action']
    text = json.dumps(result, ensure_ascii=False)
    assert '报告照片不代表影像已准备好' in text
    assert '重新做CT' not in text and '一个月后复查' not in text


def test_missing_uploads_remain_unknown_and_report_type_is_not_inferred_from_file_extension():
    empty = build_preparation(preparation_context([], []))
    assert all(row['status'] != 'provided' for row in empty['checklist'])
    assert any('不代表你没有报告' in gap for gap in empty['gaps'])
    photo = preparation_context([{'id': 'photo', 'title': 'ordinary.jpg', 'confirmed': True}], [])
    assert photo['imaging_document_ids'] == []
    assert 'images' not in {row['id'] for row in build_preparation(photo)['checklist']}


def test_only_doctor_decisions_stay_questions_and_scope_is_task_specific():
    questions = ['需要带哪些旧片？', '日常生活需要注意什么？', '需要多久复查？', '是否需要补充检查？', '需要多久复查？']
    assert focused_questions('准备复诊', questions) == ['需要多久复查？', '是否需要补充检查？']
    assert '日常生活需要注意什么？' in focused_questions('复诊及生活注意事项', questions)
    assert not preparation_requested('比较两次血糖', {'operation': 'compare'})
    assert preparation_requested('比较两次血糖并准备复诊', {'operation': 'compare'})
    result = {'preparation_checklist': [{'title': '报告', 'action': '带已有报告'}], 'questions': ['复查如何安排？']}
    assert '带已有报告' in message_text({'role': 'assistant', 'result': result})


def test_api_prepares_actions_for_no_diagnosis_and_only_the_selected_person_and_reports(tmp_path):
    async def scenario():
        repo = Repository(tmp_path/'preparation.db')
        accounts = Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app = create_app(Settings(mode='demo', recognition_provider='disabled'), repo, account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                await client.post('/auth/register', json={'username': 'prepare01', 'password': 'test123456'})
                subjects = []
                documents = []
                for name, title in [('PersonA', 'selected-ct.txt'), ('PersonB', 'other-person.txt')]:
                    person = (await client.post('/v2/subjects', json={'name': name, 'diseases': []})).json()
                    subjects.append(person)
                    raw = f'姓名：{name}\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：胸部CT平扫+增强\n影像学表现：合成术后描述。'.encode()
                    draft = (await client.post('/v2/report-reviews', params={'subject': person['id']}, files={'file': (title, raw)})).json()
                    document = (await client.post('/v2/report-reviews/'+draft['id']+'/confirm', json={'revision': draft['revision'], 'confirmed': True})).json()
                    documents.append(document)
                run_response = await client.post('/v2/runs', json={'subject': subjects[0]['id'], 'goal': '请帮我准备复诊，提醒需要带什么资料', 'document_ids': [documents[0]['id']]})
                assert run_response.status_code == 200, run_response.text
                run = run_response.json()
                for _ in range(250):
                    finished = (await client.get('/v2/runs/'+run['id'])).json()
                    if finished['status'] != 'running': break
                    await asyncio.sleep(.01)
                assert finished['status'] == 'completed', finished
                result = finished['result']
                assert result['kind'] == 'followup' and result['preparation_checklist']
                assert len(result['questions']) <= 3
                assert any(item['id'] == 'images' for item in result['preparation_checklist'])
                ids = {identifier for item in result['preparation_checklist'] for identifier in item['document_ids']}
                assert ids == {documents[0]['id']}
                assert documents[1]['id'] not in json.dumps(result)
                assert 'other-person.txt' not in json.dumps(result)
                assert not any('两个日期' in gap for gap in result['gaps'])
                conversation = (await client.get('/v2/conversations/'+finished['conversation_id'])).json()
                assert conversation['messages'][-1]['result']['preparation_checklist'] == result['preparation_checklist']
        accounts.engine.dispose()
    asyncio.run(scenario())
