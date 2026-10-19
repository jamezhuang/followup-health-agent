import asyncio
from types import SimpleNamespace
import pytest
from app.config import Settings
from app.followup.hospital import hospital_sources
from app.followup.hospital_planner import plan_hospital
from app.followup.hospital_policy import directory_policy
from app.followup.chat import chat
from tests.test_hospital_fallback import lookup, candidate, page, Repo


@pytest.mark.parametrize('goal,scope,low,high',[
    ('广东省有几个医院','province',8,10),('江西有哪些医院','province',8,10),
    ('浙江省有哪些医院','province',8,10),('深圳有哪些医院','city',5,8),
    ('新乡市有哪些医院','city',5,8),('上海有哪些医院','city',5,8),
    ('深圳福田区有哪些医院','district',4,6),('长丰县有哪些医院','district',4,6),
    ('全国有哪些医院','national',8,10),('推荐两家深圳医院','city',2,2),
])
def test_scope_policy_generalizes_without_city_specific_thresholds(goal,scope,low,high):
    plan=plan_hospital(goal)
    assert plan.kind=='directory',plan
    policy=directory_policy(plan,goal)
    assert (policy['scope'],policy['target_min'],policy['target_max'])==(scope,low,high)
    assert policy['minimum']==min(4,low)


def test_counts_and_examples_are_different_tasks_and_filters_persist():
    goal='广东省公立三甲医院总数一共有多少'
    plan=plan_hospital(goal); policy=directory_policy(plan,goal)
    assert policy['task']=='count'
    assert all('公立' in query and '三级甲等' in query and '广东' in query for query in plan.queries(policy))
    assert '统计' in plan.queries(policy)[0]
    previous=[{'role':'assistant','result':{'intent':'hospital','hospital_context':plan.context()}}]
    assert {'公立','三级甲等'}<=set(plan_hospital('深圳',previous).qualifiers)
    district=plan_hospital('深圳市福田区有哪些公立医院')
    assert district.region=='深圳市福田区'
    assert all('公立' in query for query in district.queries(directory_policy(district,'深圳市福田区有哪些公立医院')))


def test_small_page_is_followed_via_real_pagination_and_keeps_budget(monkeypatch):
    from app import web_tools
    calls=[]
    names=['合成一医院','合成二医院','合成三医院','合成四医院','合成五医院','合成六医院','合成七医院','合成八医院']
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['bocha'])
            self.browser=SimpleNamespace(enabled=False)
        async def search(self,args):
            calls.append('search')
            return {'ok':True,'candidates':[candidate('first',title='深圳医院名录')]}
        async def read(self,args):
            calls.append(args['candidate_id'])
            values=names[:4] if args['candidate_id']=='first' else names[4:]
            return page('深圳医院名录：'+'、'.join(values)+'。',args['candidate_id'],title='深圳医院名录')
        async def map(self,args):
            return {'ok':True,'candidates':[candidate('next',title='下一页'),candidate('footer',title='网站地图')]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True),Repo(),'run','深圳有哪些医院',return_details=True))
    assert calls==['search','first','next'] and result['directory_entry_count']==8
    assert result['direct_reads']==2 and not result['directory_coverage_low']
    assert result['retrieval_diagnostics']['direct_read_attempts']==2
    assert result['retrieval_diagnostics']['stop_reason']=='coverage_satisfied'


def test_statistics_body_does_not_require_a_hospital_name_list(monkeypatch):
    result,calls=lookup(monkeypatch,'广东省医院总数是多少',lambda args,n:{'ok':True,'candidates':[
        candidate(title='广东省卫生健康事业统计公报')]},lambda args,n:page(
            '广东省2025年统计公报：全省医院共1000家（合成测试数字）。',title='广东省统计公报'))
    assert result['sources'] and result['answer_policy']['task']=='count'
    assert not result['directory_below_minimum'] and len(calls['search'])==1


def test_published_local_public_knowledge_survives_external_outage():
    class LocalRepo(Repo):
        def items(self,owner,kind):
            assert (owner,kind)==('*','knowledge')
            return [{'id':'kb','revision':1,'title':'深圳医院名录','status':'published','kind':'public',
                'pieces':[{'location':'第1页','text':'深圳部分医院：合成一医院、合成二医院、合成三医院、合成四医院。'}]},
                {'id':'draft','status':'draft','kind':'public'}, {'id':'private','status':'published','kind':'private'}]
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=False),LocalRepo(),'run','深圳有哪些医院',return_details=True))
    assert result['status']=='partial' and result['external_status']=='disabled'
    assert result['sources'][0]['retrieval_origin']=='local' and result['directory_entry_count']==4


def test_generation_coverage_is_checked_and_repaired_once():
    names=['合成一医院','合成二医院','合成三医院','合成四医院','合成五医院','合成六医院','合成七医院','合成八医院']
    class Client:
        def __init__(self):self.calls=0
        async def complete(self,messages):
            assert 'directory_entry_count' not in messages[0]['content']
            self.calls+=1
            return {'content':'、'.join('**'+name+'**' for name in (names[:1] if self.calls==1 else names))}
    client=Client(); source=page('深圳：'+'、'.join(names),'public')['sources'][0]
    plan=plan_hospital('深圳有哪些医院')
    result=asyncio.run(chat(Repo(),client,True,'run','深圳有哪些医院',[],False,intent='hospital',sources=[source],
        hospital_search={'context':plan.context(),'answer_policy':directory_policy(plan,'深圳有哪些医院'),'directory_entry_count':8}))
    assert client.calls==2 and result['directory_answer_count']==8 and not result['directory_answer_below_minimum']


def test_insufficient_evidence_is_not_padded_to_meet_minimum():
    class Client:
        async def complete(self,messages):return {'content':'本次资料只能核实 **合成一医院**，资料不足。'}
    plan=plan_hospital('深圳有哪些医院')
    result=asyncio.run(chat(Repo(),Client(),True,'run','深圳有哪些医院',[],False,intent='hospital',
        sources=page('深圳：合成一医院。')['sources'],hospital_search={
            'context':plan.context(),'answer_policy':directory_policy(plan,'深圳有哪些医院'),'directory_entry_count':1}))
    assert result['directory_answer_count']==1 and result['directory_answer_below_minimum']


def test_readable_local_directory_precedes_download_landing_pages_for_any_city():
    from app.followup.hospital import _candidate_score, hospital_failure
    for region in ('广州','杭州','成都'):
        plan=plan_hospital(region+'有哪些医院')
        directory=candidate('directory',title=region+'医院大全-医院列表')
        download=candidate('download',title='全国医院名单.docx-全文可读')
        assert _candidate_score(plan,directory)>_candidate_score(plan,download)
        assert _candidate_score(plan,download)<0
        message=hospital_failure(region+'有哪些医院',{'status':'unreadable','context':plan.context()})
        assert '补充城市' not in message and '资料获取失败' in message


def test_directory_excerpt_keeps_identity_context_and_drops_doctors_and_footer():
    from app.web_documents import directory_excerpt
    text=('城市导航\n示例一医院\n示例市甲路1号\n公立\n由旧称示例旧医院更名。\n'
          '专家团队\n测试医生及详细履历。\n示例二医院\n示例市乙街2号\n公立\n'
          '由示例妇幼保健院整合而成。\n热门城市\n其他市\n其他市医院\n')
    result=directory_excerpt(text,'示例市 医院')
    assert '由旧称示例旧医院更名。' in result and '整合而成' in result
    assert '测试医生' not in result and '其他市医院' not in result
    assert '原文片段边界' in result
