import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.followup.intent import analyze_intent, IntentDecision
from app.followup.task_plan import PublicTaskPlan, PublicTask, apply_task_plan
from app.followup.hospital_planner import plan_hospital
from app.followup.hospital import hospital_sources


class Repo:
    def __init__(self): self.events = []
    def stage(self, *args, **kwargs): self.events.append((args, kwargs))


def sample_plan():
    return PublicTaskPlan(understood_request='查询医院院区、地址和交通', original_name='三水人名医院',
        search_names=['三水人民医院'], tasks=[
            PublicTask(id='campuses', topic='campuses', question='目前有几个已启用院区？', query='三水人民医院 院区数量'),
            PublicTask(id='addresses', topic='address', question='各院区地址在哪？', query='三水人民医院 各院区 地址', depends_on=['campuses']),
            PublicTask(id='transport', topic='transport', question='附近公交地铁如何到院？', query='三水人民医院 公交 地铁', depends_on=['addresses'])],
        missing_information=['出发地仅影响全程路线'])


GOAL = '三水人名医院院地址在哪？有几个院区？怎么坐车 / 地铁过来？'


def test_campus_is_not_region_and_candidate_names_are_not_separate_hospitals():
    plan = plan_hospital(GOAL)
    assert plan.region == '' and plan.regions == []
    assert apply_task_plan(plan, sample_plan(), GOAL)
    assert len(plan.tasks) == 3 and plan.entity_status == 'candidate'
    assert plan.queries()[1] == '三水人民医院 各院区 地址'
    from app.followup.hospital import _missing_names, _usable
    source = {'id': 's', 'title': '佛山市三水区人民医院', 'text': '佛山市三水区人民医院地址为合成地址。'}
    assert _usable(plan, source)
    assert not _missing_names(plan, [source])


def test_invalid_alias_private_query_and_invalid_dependencies_do_not_replace_plan():
    plan = plan_hospital(GOAL)
    invalid = sample_plan().model_dump()
    invalid['tasks'][0]['depends_on'] = ['transport']
    assert not apply_task_plan(plan, invalid, GOAL)
    assert plan.tasks == []
    invalid = sample_plan().model_dump()
    for task in invalid['tasks']: task['query'] = '三水人民医院 患者张三 病历号123456789'
    assert not apply_task_plan(plan, invalid, GOAL)
    invalid = sample_plan().model_dump()
    invalid['region'] = '有几个院区'
    assert not apply_task_plan(plan, invalid, GOAL)


def test_semantic_routing_is_not_overwritten_by_hospital_keyword():
    class Client:
        async def structured(self, schema, prompt, data):
            return IntentDecision(intent='daily', consultation_type='medical_education', reason_code='general_medical')
    result = asyncio.run(analyze_intent(Repo(), Client(), True, 'r', '医院说的检查互认是什么意思', [], False))
    assert result.consultation_type == 'medical_education'


def test_shared_plan_searches_corrected_name_without_rewrite_and_keeps_partial_results(monkeypatch):
    from app import web_tools
    queries = []
    class Tools:
        def __init__(self, *args):
            self.providers = SimpleNamespace(available=lambda: ['bocha'])
            self.browser = SimpleNamespace(enabled=False)
        async def search(self, args):
            queries.append(args['query'])
            return {'ok': True, 'candidates': [{'candidate_id': str(len(queries)),
                'title': '佛山市三水区人民医院', 'snippet': '医院地址', 'url': 'https://example.org/'+str(len(queries))}]}
        async def read(self, args):
            return {'ok': True, 'authority': 'registered', 'sources': [{'id': args['candidate_id'],
                'title': '佛山市三水区人民医院', 'text': '佛山市三水区人民医院地址：合成路1号。', 'url': 'https://example.org/address'}]}
    class Client:
        async def structured(self, schema, prompt, data):
            assert schema.__name__ != 'QueryRewrite', 'Shared plan must skip the separate rewrite agent'
            from app.followup.rag_pipeline import EvidenceReview
            return EvidenceReview(sufficient=False, source_ids=[source['id'] for source in data['sources']], missing=['quantity','other_requested_information'])
    monkeypatch.setattr(web_tools, 'WebTools', Tools)
    result = asyncio.run(hospital_sources(Settings(mode='live', external_enabled=True), Repo(), 'r', GOAL,
        return_details=True, client=Client(), shared_task_plan=sample_plan()))
    assert result['status'] == 'partial' and result['sources']
    assert len(queries) == 3 and '院区数量' in queries[0]
    assert all('人名' not in query and '有几个院区' not in query for query in queries)
    assert result['query_plan']['tasks'][1]['status'] == 'evidence_found'
    assert result['query_plan']['tasks'][0]['status'] == 'unresolved'
    assert result['query_plan']['tasks'][2]['status'] == 'unresolved'


def test_open_service_label_survives_and_user_filters_are_kept():
    goal = '广州有哪些公立三甲医院提供失物招领服务'
    plan = plan_hospital(goal)
    proposed = PublicTaskPlan(understood_request='查失物招领', region='广州', region_origin='user', tasks=[
        PublicTask(id='lost', topic='lost_property', question='失物招领怎么办理？', query='广州 公立 三甲 医院 失物招领')])
    assert apply_task_plan(plan, proposed, goal)
    assert set(plan.qualifiers) >= {'公立', '三级甲等'}
    assert plan.tasks[0]['topic'] == 'lost_property'
    assert '失物招领' in plan.queries()[0]
