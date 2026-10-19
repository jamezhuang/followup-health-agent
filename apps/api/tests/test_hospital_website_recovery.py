import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.followup.hospital import hospital_sources, retrieval_scope_mismatch, _coverage
from app.followup.hospital_planner import plan_hospital
from app.followup.task_plan import PublicTaskPlan, PublicTask, apply_task_plan
from app.followup.rag_pipeline import AnswerReview, verify_answer


GOAL = '给我他的官网网址南方医科大学珠江医院'
HISTORY = [{'role': 'assistant', 'result': {'intent': 'daily', 'consultation_type': 'hospital',
    'hospital_context': {'kind': 'directory', 'region': '珠三角地区', 'regions': ['珠三角地区'],
        'names': [], 'listed_names': ['南方医科大学珠江医院', '广州市第十二人民医院'],
        'topics': ['specialty'], 'care_topic': '韧带断裂手术'}}}]


class Repo:
    def __init__(self): self.events = []
    def stage(self, *args, **kwargs): self.events.append((args, kwargs))


def proposal():
    return PublicTaskPlan(understood_request='查珠江医院官网', original_name='南方医科大学珠江医院',
        search_names=['南方医科大学珠江医院'], region='广州', region_origin='unknown', tasks=[
            PublicTask(id='website', topic='website', question='医院官网网址是什么？', query='南方医科大学珠江医院 官网')])


def test_current_named_website_request_replaces_historical_directory_and_specialty():
    plan = plan_hospital(GOAL, HISTORY)
    assert plan.kind == 'named' and plan.names == ['南方医科大学珠江医院']
    assert plan.region == '' and plan.topics == ['website']
    assert all('珠三角' not in query and '重点专科' not in query for query in plan.queries())
    generic = plan_hospital('给我他的官网网址合成康华医院', HISTORY)
    assert generic.kind == 'named' and generic.names == ['合成康华医院']
    assert plan_hospital('给我他的官网网址', HISTORY).kind == 'clarify'


def test_guessed_region_is_removed_without_losing_current_task_and_rejection_is_diagnosed():
    plan = plan_hospital(GOAL, HISTORY)
    diagnostics = []
    assert apply_task_plan(plan, proposal(), GOAL, HISTORY, diagnostics=diagnostics)
    assert plan.region == '' and plan.topics == ['website']
    assert {'status': 'repaired', 'reason': 'inferred_region_removed'} in diagnostics
    invalid = proposal().model_dump()
    invalid['tasks'][0]['depends_on'] = ['missing']
    diagnostics = []
    assert not apply_task_plan(plan_hospital(GOAL), invalid, GOAL, HISTORY, diagnostics=diagnostics)
    assert diagnostics[-1]['reason'] == 'invalid_task_dependencies'


def test_other_campus_site_does_not_verify_main_hospital_website():
    plan = plan_hospital(GOAL)
    sources = [{'title': '南方医科大学珠江医院三水医院', 'text': '官方网站',
                'url': 'https://www.sshospital.cn/', 'authority': 'registered'}]
    assert 'website' not in _coverage(plan, sources)


def test_open_website_label_uses_task_question_for_coverage():
    plan = plan_hospital(GOAL)
    proposed = proposal().model_dump()
    proposed['tasks'][0]['topic'] = 'official_website'
    assert apply_task_plan(plan, proposed, GOAL)
    assert plan.topics == ['website']
    from app.followup.task_plan import coverage_topics
    assert coverage_topics(plan.tasks[0]) == ['website']


def test_rejected_plan_still_visits_exact_registered_website_instead_of_directory(monkeypatch):
    from app import web_tools
    reads = []
    class Tools:
        def __init__(self, *args):
            self.providers = SimpleNamespace(available=lambda: ['bocha'])
            self.browser = SimpleNamespace(enabled=False)
        def put(self, item):
            assert item['url'] == 'https://www.zjyy.com.cn/'
            return {**item, 'candidate_id': 'known'}
        async def read(self, args):
            reads.append(args['candidate_id'])
            return {'ok': False, 'error': 'page_network_error'}
        async def search(self, args):
            raise AssertionError('Known registered website must not fall back to unrelated directories')
    monkeypatch.setattr(web_tools, 'WebTools', Tools)
    invalid = proposal().model_dump()
    invalid['tasks'][0]['depends_on'] = ['missing']
    result = asyncio.run(hospital_sources(Settings(mode='demo', external_enabled=True), Repo(), 'r', GOAL, HISTORY,
        return_details=True, shared_task_plan=invalid))
    assert reads == ['known']
    assert result['context']['kind'] == 'named' and result['context']['topics'] == ['website']
    assert result['sources'][0]['url'] == 'https://www.zjyy.com.cn/'
    assert result['sources'][0]['retrieval_origin'] == 'verified_registry'
    assert result['retrieval_diagnostics']['stop_reason'] == 'official_entry_resolved'
    assert result['plan_validation'][-1]['reason'] == 'invalid_task_dependencies'


def test_wrong_retrieval_scope_requests_search_recovery_before_text_only_repair():
    class Client:
        async def structured(self, schema, prompt, data):
            return AnswerReview(passed=False, issues=['wrong_question', 'wrong_scope'])
        async def complete(self, *args):
            raise AssertionError('Do not repair the answer using irrelevant directory evidence')
    wrong_context = HISTORY[0]['result']['hospital_context']
    assert retrieval_scope_mismatch(GOAL, wrong_context)
    assert not retrieval_scope_mismatch(GOAL, plan_hospital(GOAL).context())
    _, review = asyncio.run(verify_answer(Client(), Repo(), 'r', GOAL,
        [{'id': 's', 'title': '珠三角医院名录', 'text': '医院名单'}], '医院名单如下', retrieval_context=wrong_context))
    assert review['status'] == 'retrieval_required'
