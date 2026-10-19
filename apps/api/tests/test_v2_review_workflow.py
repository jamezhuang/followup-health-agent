import asyncio
import json
import httpx
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.clinical_fields import ExtractedReport, Observation, DoctorTask
from app.followup.memory import context_packet, public_history
from app.followup.intent import fallback_intent
from app.followup.retrieval import chunks
from app.v2_main import create_app
from app.followup.scope import record_scope
from app.followup.workflow import Workflow, Plan, Review, Draft, Claim, Verification, EvidenceRanking
from types import SimpleNamespace


def test_durable_approval_ownership_conflicts_idempotency_and_provenance(tmp_path):
    class Model:
        async def structured(self, schema, instruction, data):
            assert schema is ExtractedReport
            piece=data['pieces'][0]
            return ExtractedReport(patient_name='张父',examination_date='2026-10-10',summary='本次检查记录。',
                observations=[Observation(label='空腹血糖',value='6.2',unit='',location=piece['location'],quote='空腹血糖 6.2'),
                              Observation(label='收缩压',value='135',unit='mmHg',location=piece['location'],quote='收缩压 135 mmHg')],
                doctor_tasks=[DoctorTask(content='携带血压记录复诊',location=piece['location'],quote='医嘱：携带血压记录复诊')])
    async def scenario():
        repo=Repository(tmp_path/'review.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app=create_app(Settings(mode='live'),repo,Model(),accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as other:
                await api.post('/auth/register',json={'username':'review01','password':'123456'})
                await other.post('/auth/register',json={'username':'review02','password':'123456'})
                sid=(await api.post('/v2/subjects',json={'name':'爸爸','patient_name':'张父','diseases':['diabetes']})).json()['id']
                raw='姓名：张父\n检查日期：2026-10-10\n空腹血糖 6.2\n收缩压 135 mmHg\n医嘱：携带血压记录复诊'.encode()
                draft=(await api.post('/v2/report-reviews',params={'subject':sid,'report_type':'followup'},files={'file':('报告.txt',raw)})).json()
                assert 'data' not in draft and draft['status']=='pending'
                assert (await api.get('/v2/records',params={'subject':sid})).json()['items']==[]
                assert (await other.get('/v2/report-reviews/'+draft['id'])).status_code==404
                assert (await api.post('/v2/report-reviews/'+draft['id']+'/confirm',json={'revision':1,'confirmed':False})).status_code==422
                # A new app/repository instance recovers the same pending review, never auto-confirms it.
                assert Repository(tmp_path/'review.db').items('invalid','report_review')==[]
                assert (await api.get('/v2/report-reviews',params={'subject':sid})).json()['items'][0]['id']==draft['id']
                edit={**draft['fields'],'title':draft['title'],'revision':draft['revision'],'report_type':'followup',
                      'update_current':True,'save_tasks':True,'expected_current_id':''}
                edit['observations'][0]['unit']='mmol/L'
                updated=(await api.put('/v2/report-reviews/'+draft['id'],json=edit)).json()
                stale=await api.post('/v2/report-reviews/'+draft['id']+'/confirm',json={'revision':1,'confirmed':True})
                assert stale.status_code==409
                body={'revision':updated['revision'],'confirmed':True}
                saved=(await api.post('/v2/report-reviews/'+draft['id']+'/confirm',json=body)).json()
                replay=(await api.post('/v2/report-reviews/'+draft['id']+'/confirm',json=body)).json()
                assert saved['id']==replay['id']
                records=(await api.get('/v2/records',params={'subject':sid})).json()['items']
                assert len(records)==2
                assert next(r for r in records if r['label']=='空腹血糖')['unit_source']=='user_confirmed'
                assert next(r for r in records if r['label']=='收缩压')['unit_source']=='report'
                assert all(r['source_document_id']==saved['id'] for r in records)
                todos=(await api.get('/v2/todos',params={'subject':sid})).json()['items']
                assert len(todos)==1 and todos[0]['due_date']==''
                assert (await api.get('/v2/followup-reports',params={'subject':sid})).json()['current']['summary']=='本次检查记录。'
                assert len((await api.get('/v2/report-events',params={'subject':sid})).json()['items'])==1
                assert (await api.get('/v2/followup-reports/'+saved['id']+'/file')).content==raw
                assert (await api.put('/v2/documents/'+saved['id'],json={'subject':sid,'title':'覆盖','pieces':draft['pieces'],'revision':1})).status_code==409
                # Missing report date never turns upload time into an observation date.
                second=(await api.post('/v2/report-reviews',params={'subject':sid},files={'file':('补充.txt',raw)})).json()
                missing={**second['fields'],'examination_date':'','title':second['title'],'revision':1,'report_type':'inspection'}
                second=(await api.put('/v2/report-reviews/'+second['id'],json=missing)).json()
                response=await api.post('/v2/report-reviews/'+second['id']+'/confirm',json={'revision':second['revision'],'confirmed':True})
                assert response.status_code==200
                assert len((await api.get('/v2/records',params={'subject':sid})).json()['items'])==2
                await api.delete('/v2/subjects/'+sid)
                with repo.db() as db:
                    assert db.execute('SELECT COUNT(*) FROM entities WHERE subject=?',(sid,)).fetchone()[0]==0
                    assert db.execute('SELECT COUNT(*) FROM report_events WHERE subject=?',(sid,)).fetchone()[0]==0
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_identity_and_current_version_conflicts_do_not_partially_commit(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'identity.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app=create_app(Settings(mode='demo'),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
                await api.post('/auth/register',json={'username':'review03','password':'123456'})
                sid=(await api.post('/v2/subjects',json={'name':'父亲','diseases':['hypertension']})).json()['id']
                draft=(await api.post('/v2/report-reviews',params={'subject':sid},files={'file':('a.txt','姓名：另一人\n收缩压 135 mmHg'.encode())})).json()
                assert (await api.post('/v2/report-reviews/'+draft['id']+'/confirm',json={'revision':1,'confirmed':True})).status_code==422
                assert (await api.get('/v2/documents',params={'subject':sid})).json()['items']==[]
                edit={**draft['fields'],'title':'a.txt','revision':1,'report_type':'followup','summary':'核对信息','update_current':True,'expected_current_id':''}
                draft=(await api.put('/v2/report-reviews/'+draft['id'],json=edit)).json()
                owner=repo.items('invalid','subject')  # owner identifiers are never accepted from request bodies
                with repo.db() as db:
                    account_id=db.execute("SELECT owner FROM entities WHERE id=?",(sid,)).fetchone()['owner']
                repo.create(account_id,'followup_info',sid,{'summary':'并发更新','document_id':'older'})
                failed=await api.post('/v2/report-reviews/'+draft['id']+'/confirm',json={'revision':draft['revision'],'confirmed':True,'identity_confirmed':True})
                assert failed.status_code==409
                assert (await api.get('/v2/documents',params={'subject':sid})).json()['items']==[]
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_long_dialogue_constraints_survive_compression_without_private_history_leak():
    short=context_packet([{'role':'user','content':'只看父亲，不要调整药物。'}])
    assert '不要调整药物' in str(short['capsule']['constraints'])
    messages=[{'role':'user','content':'只看爸爸的资料，不要调整药物；妈妈的记录不能混入。'}]
    for index in range(90):
        messages.extend([{'role':'user','content':f'第{index}次讨论记录。'+'资料说明。'*70},
                         {'role':'assistant','result':{'kind':'chat','content':'本轮仅整理已有资料。'*50}}])
    context=context_packet(messages,'整理爸爸复诊',budget=12000)
    assert context['level']==3 and not context['requires_narrowing']
    assert '不要调整药物' in json.dumps(context['capsule'],ensure_ascii=False)
    assert len(context['history'])==4 and context['count_method']=='utf8_upper_bound'
    assert context['prepared_estimate']<context['input_estimate']
    messages=[{'role':'user','content':'PRIVATE_REPORT'}, {'role':'assistant','result':{'kind':'followup','claims':[]}}, {'role':'user','content':'抗生素是什么'}]
    assert 'PRIVATE_REPORT' not in str(public_history(messages))


def test_hospital_route_and_structure_preserving_chunks():
    assert fallback_intent('北京协和医院如何预约挂号',[]).response_mode=='hospital'
    text='检查项 | 数值 | 单位\n'+'空腹血糖 | 6.2 | mmol/L\n'*100
    result=chunks({'id':'report','revision':1,'title':'报告','pieces':[{'location':'表格1','text':text}]})
    assert all(row['text'] in row['parent'] for row in result)
    assert all(row['parent_id']=='report:1:0' for row in result)
    assert all(row['location']=='表格1' for row in result)


def test_explicit_date_and_project_scope_uses_actual_record_dates():
    rows=[{'date':'2026-10-01','label':'收缩压'}, {'date':'2026-10-16','label':'收缩压'},
          {'date':'2026-10-17','label':'舒张压'}, {'date':'2026-10-17','label':'空腹血糖'}]
    selected,scope=record_scope(rows,'只看2026年10月15日到2026年10月17日的血压变化')
    assert len(selected)==2 and all(row['label'] in {'收缩压','舒张压'} for row in selected)
    assert scope['from']=='2026-10-15'
    selected,_=record_scope(rows,'从2026-10-17以来的空腹血糖')
    assert selected==[rows[-1]]


def test_requested_public_section_survives_reranking_and_answer_omission_is_repaired(tmp_path):
    class Model:
        settings=SimpleNamespace(embedding_key='',rag_rerank_enabled=True)
        def __init__(self):self.generations=0
        async def structured(self,schema,instruction,data):
            if schema is Plan:return Plan(queries=['记录','饮食'],focus='准备复诊')
            if schema is EvidenceRanking:return EvidenceRanking(ordered_ids=[])
            if schema is Review:return Review(action='generate',relevant_ids=[])
            if schema is Draft:
                self.generations+=1
                if self.generations==1:return Draft()
                evidence=data['evidence'][0]
                return Draft(claims=[Claim(text='指南提出监测血压、自我管理。',quote='监测血压，自我管理。',evidence_id=evidence['id'])])
            if schema is Verification:return Verification(supported_indices=[0])
    async def scenario():
        repo=Repository(tmp_path/'coverage.db');model=Model()
        source={'id':'public','revision':1,'title':'合成指南','kind':'public','pieces':[{'location':'第1页','text':'监测血压，自我管理。'}]}
        result=await Workflow(repo,model,True).run('coverage','根据《合成指南》“监测血压，自我管理”段落提供逐字出处',
            {'diseases':['hypertension'],'range':'历次','constraints':'不调药','records':[],'chunks':chunks(source)})
        assert model.generations==2 and len(result['claims'])==1
        assert result['sources'][0]['kind']=='public'
        assert any(stage['action']=='补齐指定资料的回答' for stage in repo.trace('coverage')['stages'])
    asyncio.run(scenario())
