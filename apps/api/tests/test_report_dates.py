import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.storage import Store
from app.followup.clinical_fields import rule_extract
from app.followup.store import Repository
from app.followup.retrieval import chunks
from app.v2_main import create_app


@pytest.mark.parametrize('text,examination,issued', [
    ('报告日期：2026-07-09\n检查日期：2026-07-07', '2026-07-07', '2026-07-09'),
    ('检查日期：2026/7/7', '2026-07-07', ''),
    ('报告日期：2026年7月9日', '', '2026-07-09'),
    ('检查日期：2026-02-30\n报告日期：2026-03-01', '', '2026-03-01'),
])
def test_date_labels_are_independent(text, examination, issued):
    fields = rule_extract([{'location': 'test', 'text': text}])
    assert fields.examination_date == examination
    assert fields.report_date == issued


def test_legacy_confirmed_date_stays_examination_date_without_rewriting_history(tmp_path):
    repo = Repository(tmp_path/'legacy.db')
    legacy = {'report_date': '2026-07-07', 'observations': [], 'summary': ''}
    document = repo.create('owner', 'document', 'subject', {
        'title': 'legacy', 'confirmed': True, 'pieces': [], 'key_fields': legacy,
    })
    assert document['key_fields']['examination_date'] == '2026-07-07'
    assert document['key_fields']['report_date'] == ''
    assert repo.get('owner', 'document', document['id'])['key_fields'] == document['key_fields']
    with repo.db() as db:
        stored = json.loads(db.execute('SELECT body FROM entities WHERE id=?', (document['id'],)).fetchone()['body'])
    assert stored['key_fields'] == legacy
    text = '\n'.join(row['text'] for row in chunks({**document, 'key_fields': legacy}))
    assert '实际检查或就诊日期：2026-07-07' in text
    assert '报告出具日期：未确认' in text


def test_confirm_and_restore_dates_use_only_examination_date_for_records(tmp_path):
    async def scenario():
        repo = Repository(tmp_path/'dates.db')
        accounts = Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app = create_app(Settings(mode='demo', recognition_provider='disabled'), repo, account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                await client.post('/auth/register', json={'username': 'dates01', 'password': 'test123456'})
                subject = (await client.post('/v2/subjects', json={'name': 'Test', 'diseases': ['diabetes']})).json()['id']
                for exam, issued in [('', '2026-07-09'), ('2026-07-07', '2026-07-10')]:
                    raw = f'姓名：Test\n报告日期：{issued}\n检查日期：{exam}\n空腹血糖 6.2 mmol/L'.encode()
                    draft = (await client.post('/v2/report-reviews', params={'subject': subject}, files={'file': ('test.txt', raw)})).json()
                    edit = {**draft['fields'], 'title': draft['title'], 'report_type': 'inspection', 'revision': draft['revision']}
                    invalid = await client.put('/v2/report-reviews/'+draft['id'], json={**edit, 'report_date': '2026-02-30'})
                    assert invalid.status_code == 422
                    # Saving and reopening a draft retains the two independent values.
                    saved = (await client.put('/v2/report-reviews/'+draft['id'], json=edit)).json()
                    restored = (await client.get('/v2/report-reviews/'+draft['id'])).json()
                    assert restored['fields']['examination_date'] == exam
                    assert restored['fields']['report_date'] == issued
                    response = await client.post('/v2/report-reviews/'+draft['id']+'/confirm', json={'revision': saved['revision'], 'confirmed': True})
                    assert response.status_code == 200, response.text
                    document = response.json()
                    assert document['followup_date'] == exam
                    assert document['key_fields']['report_date'] == issued
                    records = (await client.get('/v2/records', params={'subject': subject})).json()['items']
                    assert len(records) == (1 if exam else 0)
                    assert all(row['date'] == '2026-07-07' for row in records)
        accounts.engine.dispose()
    asyncio.run(scenario())
