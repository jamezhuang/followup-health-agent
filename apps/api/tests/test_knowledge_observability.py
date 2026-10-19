import asyncio,json
import httpx
from app.config import Settings
from app.storage import Store
from app.v2_main import create_app
from app.followup.store import Repository


def test_empty_stats_and_no_unverified_success(tmp_path):
    repo=Repository(tmp_path/'business.db')
    empty=repo.knowledge_observability()
    assert empty['citations']['pass_rate'] is None
    assert empty['queues']['import']['queued']==0
    for status in ['passed','repaired','failed','failed_after_answer_review','unavailable','not_applicable',None]:
        repo.create('private-owner','run','private-patient',{'status':'completed','goal':'PRIVATE QUESTION',
            'result':{'content':'PRIVATE ANSWER','sources':[{'text':'PRIVATE REPORT'}],
                      'citation_review':{'status':status,'answer_kind':'partial'}}})
    summary=repo.knowledge_observability()
    assert summary['citations']['attempted']==5
    assert summary['citations']['passed']==2 and summary['citations']['pass_rate']==.4
    assert len(summary['citations']['recent_failures'])==3
    payload=json.dumps(summary)
    assert all(secret not in payload for secret in ['PRIVATE','private-owner','private-patient','sources','goal','content'])


def test_window_is_latest_100_runs_and_not_all_history(tmp_path):
    repo=Repository(tmp_path/'business.db')
    repo.create('x','run','',{'result':{'citation_review':{'status':'passed'}}})
    for _ in range(100):repo.create('x','run','',{'result':{}})
    stats=repo.knowledge_observability()['citations']
    assert stats['run_count']==100 and stats['passed']==0 and stats['pass_rate'] is None


def test_admin_dashboard_has_queue_failures_without_file_or_patient_content(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'business.db')
        accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('statsadmin','test123456',role='admin')
        app=create_app(Settings(mode='demo',external_enabled=False,embedding_key=''),repo,account_store=accounts)
        # No workers needed: set fixture task metadata without private payload serialization.
        with repo.db() as db:
            db.execute("INSERT INTO knowledge_imports VALUES ('synthetic-import','SECRET FILE.txt','hash',?,'failed','failed','utf8_required','',1,'2026-10-19','2026-10-19')",(b'SECRET FILE BODY',))
            db.execute("INSERT INTO knowledge_publications VALUES ('synthetic-publish','synthetic-doc',1,'SECRET TITLE','failed','failed',0,1,2,'publication_stale','2026-10-19','2026-10-19')")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.get('/v2/admin/dashboard')).status_code==401
            await client.post('/auth/login',json={'username':'statsadmin','password':'test123456'})
            response=await client.get('/v2/admin/dashboard')
            assert response.status_code==200,response.text
            stats=response.json()['knowledge_observability']
            assert stats['queues']['import']['failed']==1 and stats['queues']['publication']['failed']==1
            assert len(stats['recent_failures'])==2 and 'SECRET' not in json.dumps(stats)
            await client.post('/auth/register',json={'username':'statsuser','password':'test123456'})
            assert (await client.get('/v2/admin/dashboard')).status_code==403
        accounts.engine.dispose()
    asyncio.run(scenario())
