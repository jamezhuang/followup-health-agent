import asyncio
import httpx
import pytest

from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.clinical_fields import rule_extract, grounded_extraction, ExtractedReport, AdditionalReportField, structure_examination
from app.followup.retrieval import chunks
from app.v2_main import create_app


@pytest.mark.parametrize('raw,expected,body,kind,method', [
    ('[薄层扫描、薄层扫描][胸部,CT平扫+增强]', '胸部 CT 平扫＋增强', '胸部', 'CT', '平扫＋增强'),
    ('胸部CT平扫', '胸部 CT 平扫', '胸部', 'CT', '平扫'),
    ('盆腔MR平扫+增强+弥散成像', '盆腔 MR 平扫＋增强＋弥散成像', '盆腔', 'MR', '平扫＋增强＋弥散成像'),
    ('胸部CT', '胸部 CT', '胸部', 'CT', ''),
    ('胸部CT非增强', '胸部 CT 非增强', '胸部', 'CT', '非增强'),
    ('血常规', '血常规', '', '', ''),
])
def test_combined_project_preserves_explicit_components(raw, expected, body, kind, method):
    pieces = [{'location': '1', 'text': '检查项目：'+raw}]
    details = grounded_extraction(rule_extract(pieces), pieces)['details']
    assert details['examination_name'] == expected
    assert details['examination_body_part'] == body
    assert details['examination_modality'] == kind
    assert details['examination_method'] == method
    assert raw in details['examination_original']
    assert structure_examination(details) == details
    if '薄层' in raw: assert details['examination_technical_details'] == '薄层扫描'


def test_project_edit_replaces_stale_hidden_components():
    details = structure_examination({'examination_name': '胸部CT平扫+增强'})
    original = details['examination_original']
    changed = structure_examination({**details, 'examination_name': '腹部 MR 平扫'}, edited_name=True)
    assert changed['examination_body_part'] == '腹部'
    assert changed['examination_modality'] == 'MR'
    assert changed['examination_method'] == '平扫'
    assert changed['examination_original'] == original
    laboratory = structure_examination({**changed, 'examination_name': '血常规'}, edited_name=True)
    assert laboratory['examination_name'] == '血常规'
    assert laboratory['examination_modality'] == laboratory['examination_method'] == laboratory['examination_body_part'] == ''


def test_imaging_template_keeps_findings_dates_signatures_and_unmodeled_fields():
    text = ('合成医院\nCT检查报告单\n姓名：测试患者\n性别：女 年龄：30岁 科室：呼吸科\n'
            '病人号：P123 检查号：CT456 申请单号：A789\n检查部位：胸部\n检查方法：CT平扫\n'
            '检查日期：2026-07-06 14:41:14\n报告日期：2026-07-07 14:55:51\n层厚：5mm\n'
            '影像学表现：\n此处为合成影像所见，不对应真实患者。\n影像学意见：\n此处为合成报告结论。\n'
            '建议：此处为合成复查建议。\n报告医生：测试医生 审核医生：审核医生甲\n')
    pieces = [{'location': '第1页', 'text': text}]
    fields = grounded_extraction(rule_extract(pieces), pieces)
    assert fields['institution'] == '合成医院'
    assert fields['examination_date'] == '2026-07-06' and fields['report_date'] == '2026-07-07'
    detail = fields['details']
    assert detail['report_title'] == 'CT检查报告单'
    assert detail['patient_sex'] == '女' and detail['patient_age'] == '30岁'
    assert detail['patient_number'] == 'P123' and detail['application_number'] == 'A789'
    assert detail['examination_time'] == '14:41:14' and detail['report_time'] == '14:55:51'
    assert '合成影像所见' in detail['findings'] and '合成报告结论' in detail['impression']
    assert detail['reporting_doctor'] == '测试医生' and detail['reviewing_doctor'] == '审核医生甲'
    assert detail['inpatient_number'] == '' and detail['technician'] == ''
    assert any(item['label'] == '层厚' and item['value'] == '5mm' for item in fields['additional_fields'])
    verified = '\n'.join(item['text'] for item in chunks({'id': 'sample', 'revision': 1, 'title': 'synthetic', 'confirmed': True, 'pieces': pieces, 'key_fields': fields}))
    assert '已核对检查所见' in verified and '自动补充字段 层厚：5mm' in verified
    assert '未经患者逐项核对' in verified and '已核对补充字段' not in verified


def test_mr_narrative_and_unknown_values_are_not_guessed():
    pieces = [{'location': '1', 'text': 'MR诊断报告书\n临床病史：合成病史\n对比旧片：无\n影像学表现：\n合成所见。\n意见：合成意见。\n审核时间：2025年12月15日20:32:35'}]
    result = grounded_extraction(rule_extract(pieces), pieces)
    assert result['details']['comparison_info'] == '无'  # Explicit source text is distinct from an absent field.
    assert result['details']['reporting_doctor'] == ''
    assert result['report_date'] == ''  # Review time cannot silently become report date.
    invented = ExtractedReport(details={'findings': '编造的发现'}, additional_fields=[AdditionalReportField(label='未知项目', value='编造值', location='1', quote='合成所见。')])
    checked = grounded_extraction(invented, pieces)
    assert checked['details']['findings'] == '' and checked['additional_fields'] == []
    additional = grounded_extraction(rule_extract([{'location': '1', 'text': '对比剂：\n层厚：5mm'}]), [{'location': '1', 'text': '对比剂：\n层厚：5mm'}])['additional_fields']
    assert any(item['label'] == '对比剂' and item['value'] == '' for item in additional)


@pytest.mark.parametrize('role', ['user', 'admin'])
def test_profile_and_report_template_roundtrip_for_both_roles(tmp_path, role):
    async def scenario():
        repo = Repository(tmp_path/'profiles.db')
        accounts = Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('profile01', 'test123456', role=role)
        app = create_app(Settings(mode='demo', recognition_provider='disabled'), repo, account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                login = await client.post('/auth/login', json={'username': 'profile01', 'password': 'test123456'})
                assert login.json()['role'] == role
                body = {'name': '测试对象', 'patient_name': '测试患者', 'sex': '女', 'birth_date': '1990-01-02', 'age': '36岁',
                        'diseases': [], 'medical_history': '合成既往史', 'allergies': '待核对', 'other_conditions': '影像复查', 'additional_info': '合成补充资料'}
                response = await client.post('/v2/subjects', json=body)
                assert response.status_code == 200, response.text
                subject = response.json()
                assert all(subject[key] == value for key, value in body.items())
                raw = '姓名：测试患者\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：胸部CT平扫+增强\n影像学表现：合成所见\n影像学意见：合成结论\n层厚：5mm'.encode()
                draft = (await client.post('/v2/report-reviews', params={'subject': subject['id']}, files={'file': ('sample.txt', raw)})).json()
                edit = {**draft['fields'], 'title': draft['title'], 'report_type': 'inspection', 'revision': draft['revision']}
                edit['details']['findings'] = '用户核对更正后的所见'
                edit['details']['examination_name'] = '腹部 MR 平扫'
                edit['additional_fields'].append({'label': '手动备注', 'value': '补充已核对资料', 'location': '', 'quote': ''})
                saved = await client.put('/v2/report-reviews/'+draft['id'], json=edit)
                assert saved.status_code == 200, saved.text
                restored = (await client.get('/v2/report-reviews/'+draft['id'])).json()
                assert restored['fields']['details']['findings'] == '用户核对更正后的所见'
                result = await client.post('/v2/report-reviews/'+draft['id']+'/confirm', json={'revision': saved.json()['revision'], 'confirmed': True})
                assert result.status_code == 200, result.text
                assert any(item['label'] == '层厚' for item in result.json()['key_fields']['additional_fields'])
                assert not any(item['label'] == '手动备注' for item in result.json()['key_fields']['additional_fields'])
                assert result.json()['key_fields']['details']['findings'] == '用户核对更正后的所见'
                assert result.json()['key_fields']['details']['examination_name'] == '腹部 MR 平扫'
                assert result.json()['key_fields']['details']['examination_body_part'] == '腹部'
                assert result.json()['key_fields']['details']['examination_modality'] == 'MR'
                assert result.json()['key_fields']['details']['examination_method'] == '平扫'
        accounts.engine.dispose()
    asyncio.run(scenario())
