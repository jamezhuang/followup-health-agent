import asyncio
import json
import httpx
import pytest
from app.config import Settings
from app.followup.intent import IntentDecision, fallback_intent, analyze_intent
from app.followup.hospital_planner import plan_hospital
from app.followup.store import Repository
from app.storage import Store
from app.v2_main import create_app


@pytest.mark.parametrize('goal,branch,mode,operation',[
    ('癌症是什么','daily','medical_education','answer'),
    ('化疗是什么','daily','medical_education','answer'),
    ('我妈血压高怎么办','daily','medical_education','answer'),
    ('广东省有那些医院','daily','hospital','answer'),
    ('湖南省有那些医院比较擅长治疗韧带撕裂手术','daily','hospital','answer'),
    ('我如果要去做化疗应该去那个医院比较好','daily','hospital','answer'),
    ('提取上次复诊信息','followup','followup','retrieve'),
    ('把这份报告归档','followup','followup','archive'),
    ('不要保存报告，只解读这份报告','followup','followup','answer'),
    ('帮我准备复诊清单','followup','followup','prepare'),
])
def test_two_branch_scope(goal,branch,mode,operation):
    decision=fallback_intent(goal,[])
    assert decision.intent==branch and decision.response_mode==mode
    assert decision.operation==operation


def test_only_two_primary_branches_in_model_schema():
    assert IntentDecision.model_json_schema()['properties']['intent']['enum']==['daily','followup']


def test_oral_hospital_choice_does_not_search_a_pronoun_as_institution():
    plan=plan_hospital('我如果要去做化疗应该去那个医院比较好')
    assert not plan.names and plan.kind=='clarify' and plan.care_topic=='化疗'
    assert '疾病' in plan.clarification and '城市' in plan.clarification


def test_injury_direction_and_new_hospital_context_are_preserved():
    plan=plan_hospital('湖南省有那些医院比较擅长治疗韧带撕裂手术')
    assert plan.region=='湖南省' and plan.care_topic=='韧带撕裂'
    assert all('湖南省' in q for q in plan.queries())
    assert '韧带撕裂' in plan.queries()[0] and '手术' in plan.queries()[0]
    assert plan.treatment=='手术' and '运动医学科' in plan.queries()[-1]
    history=[{'role':'assistant','result':{'intent':'daily','consultation_type':'hospital',
        'hospital_context':{'kind':'directory','region':'广州','names':[],'topics':[], 'care_topic':'乳腺癌'}}}]
    follow=plan_hospital('化疗去哪个医院比较好',history)
    assert follow.region=='广州' and follow.care_topic=='乳腺癌' and not follow.names
    assert follow.treatment=='化疗' and '化疗' in follow.queries()[0]
    assert all('广州' in q for q in follow.queries())


def test_semantic_consultation_can_handle_unlisted_phrasing(tmp_path):
    class Client:
        async def structured(self,schema,instruction,data):
            return schema(intent='daily',consultation_type='medical_education',reason_code='general_medical')
    decision=asyncio.run(analyze_intent(Repository(tmp_path/'r.db'),Client(),True,'r','为什么会觉得天旋地转',[],False))
    assert decision.intent=='daily' and decision.consultation_type=='medical_education'


def test_semantic_source_matching_requires_verbatim_quotes_and_current_scope(tmp_path):
    from app.followup.rag_pipeline import match_hospital_sources,HospitalMatches
    from app.followup.hospital import _usable
    plan=plan_hospital('湖南省有那些医院比较擅长治疗韧带撕裂手术')
    source={'id':'s','title':'合成大学附属医院运动医学科','text':'地址：湖南省长沙市。开展韧带损伤诊疗及手术。'}
    class Client:
        async def structured(self,schema,prompt,data):
            assert schema is HospitalMatches
            return schema(matches=[
                {'source_id':'s','institution':'合成大学附属医院','region_quote':'湖南省长沙市','focus_quote':'韧带损伤诊疗','treatment_quote':'开展韧带损伤诊疗及手术'},
                {'source_id':'invented','institution':'不存在医院','region_quote':'湖南省','focus_quote':'韧带撕裂','treatment_quote':'手术'},
                {'source_id':'s','institution':'合成大学附属医院','region_quote':'湖南省长沙市','focus_quote':'全国最擅长韧带重建','treatment_quote':'手术'},
            ])
    accepted=asyncio.run(match_hospital_sources(Client(),Repository(tmp_path/'quotes.db'),'r',plan,[source]))
    assert len(accepted)==1 and _usable(plan,accepted[0])
    assert not _usable(plan_hospital('广东省有那些医院比较擅长治疗韧带撕裂手术'),accepted[0])


def test_department_discovery_does_not_replace_the_original_care_request(tmp_path):
    from app.followup.rag_pipeline import rewrite_hospital
    plan=plan_hospital('湖南省有那些医院比较擅长治疗韧带撕裂手术')
    class Client:
        async def structured(self,schema,prompt,data):
            return schema(queries=['湖南省 韧带撕裂 手术 医院 科室介绍'],related_departments=['运动医学科','骨科'])
    queries=asyncio.run(rewrite_hospital(Client(),Repository(tmp_path/'departments.db'),'r',plan,None))
    assert len(queries)==3 and all('湖南省' in q for q in queries)
    assert '手术' in queries[0] and plan.treatment=='手术'
    assert '韧带撕裂' in queries[0] and '运动医学科' in queries[-1]


def test_archive_and_retrieve_use_selected_owner_scope_and_never_auto_save(tmp_path):
    async def main():
        repo=Repository(tmp_path/'v2.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app=create_app(Settings(mode='demo',external_enabled=False),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
                await api.post('/auth/register',json={'username':'archivetest','password':'123456'})
                sid=(await api.post('/v2/subjects',json={'name':'自己','diseases':[]})).json()['id']
                other=(await api.post('/v2/subjects',json={'name':'另一个对象','diseases':[]})).json()['id']
                await api.post('/v2/documents',json={'subject':sid,'title':'已存复诊报告','confirmed':True,'pieces':[{'location':'原文','text':'SELECTED_SAVED_REPORT'}]})
                await api.post('/v2/documents',json={'subject':other,'title':'另一对象报告','confirmed':True,'pieces':[{'location':'原文','text':'OTHER_SUBJECT_SECRET'}]})
                async def turn(goal,subject):
                    row=(await api.post('/v2/runs',json={'goal':goal,'subject':subject})).json()
                    for _ in range(300):
                        await asyncio.sleep(.005)
                        row=(await api.get('/v2/runs/'+row['id'])).json()
                        if row['status']!='running':break
                    assert row['status']=='completed',row
                    return row['result']
                result=await turn('提取上次复诊信息',sid)
                assert result['intent']=='followup' and result['operation']=='retrieve'
                assert 'SELECTED_SAVED_REPORT' in result['content'] and 'OTHER_SUBJECT_SECRET' not in json.dumps(result)
                assert not result.get('preparation_checklist')
                result=await turn('提取上次复诊信息','')
                assert 'SELECTED_SAVED_REPORT' not in json.dumps(result) and not result['sources']
                with repo.db() as db:
                    before=db.execute("SELECT id,body,revision FROM entities WHERE kind IN ('document','subject','record','followup_info') ORDER BY id").fetchall()
                    before=[tuple(r) for r in before]
                result=await turn('把这份报告归档',sid)
                assert result['operation']=='archive' and '核对' in result['content']
                assert result['stop_reason']=='confirmation_required'
                assert not result.get('preparation_checklist')
                with repo.db() as db:
                    after=[tuple(r) for r in db.execute("SELECT id,body,revision FROM entities WHERE kind IN ('document','subject','record','followup_info') ORDER BY id")]
                assert before==after
        accounts.engine.dispose()
    asyncio.run(main())
