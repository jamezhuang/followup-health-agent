import asyncio
import inspect
from types import SimpleNamespace
import pytest
from app.config import Settings
from app.followup.hospital import hospital_sources, hospital_failure
from app.followup.hospital_planner import plan_hospital
from app.followup.intent import fallback_intent
from app.followup.chat import chat


class Repo:
    def stage(self,*args,**kwargs): pass


def candidate(identifier='one',title='示例医院',snippet='科室信息'):
    return {'candidate_id':identifier,'title':title,'snippet':snippet,'url':'https://example.com/'+identifier}


def page(text,identifier='source',title='医院介绍'):
    return {'ok':True,'authority':'unverified','sources':[{'id':identifier,'title':title,'text':text,'url':'https://example.com/'+identifier}]}


def lookup(monkeypatch, goal, search, reader=None, *, browser=False, providers=None, messages=None, settings=None):
    from app import web_tools
    calls={'search':[],'read':[]}
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:providers or ['bocha'])
            self.browser=SimpleNamespace(enabled=browser)
        async def search(self,args):
            calls['search'].append(args)
            return search(args,len(calls['search']))
        async def read(self,args):
            calls['read'].append(args)
            result=reader(args,len(calls['read'])) if reader else page('示例医院设内科。')
            return await result if inspect.isawaitable(result) else result
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(hospital_sources(settings or Settings(mode='demo',external_enabled=True),Repo(),'run',goal,messages,return_details=True))
    return result,calls


@pytest.mark.parametrize('goal,kind,names,region',[
    ('广东省有那些医院','directory',[],'广东省'),
    ('广州有哪些医院','directory',[],'广州'),
    ('广州医院有哪些','directory',[],'广州'),
    ('普宁康美医院有哪些科室','named',['普宁康美医院'],''),
    ('中山大学附属第一医院地址在哪','named',['中山大学附属第一医院'],''),
    ('广东省人民医院重点科室和位置','named',['广东省人民医院'],''),
    ('上海儿童医学中心周末开门吗','named',['上海儿童医学中心'],''),
    ('医院一般怎么挂号','general',[],''),
    ('人民医院在哪里','clarify',[],''),
])
def test_plan_distinguishes_directory_institution_and_general_service(goal,kind,names,region):
    plan=plan_hospital(goal)
    assert plan.kind==kind and plan.names==names and plan.region==region,plan


def test_regional_query_reads_relevant_body_instead_of_matching_whole_question(monkeypatch):
    result,calls=lookup(monkeypatch,'广东省有那些医院',
        lambda args,n:{'ok':True,'candidates':[candidate(title='广东省卫生健康委员会 医疗机构名录',snippet='广东省医院目录')]},
        lambda args,n:page('广东省部分医院：广东省人民医院、南方医院。',title='广东省医院名录'))
    assert result['status']=='partial' and result['sources']
    assert len(calls['search'])==3 and len(calls['read'])==1
    assert '有那些' not in calls['search'][0]['query']
    assert not result['exhaustive'] and result['sources'][0]['authority']=='unverified'


def test_empty_results_rewrite_without_repeating_query(monkeypatch):
    result,calls=lookup(monkeypatch,'示例医院重点科室',lambda args,n:
        {'ok':True,'candidates':[] if n==1 else [candidate()]},
        lambda args,n:page('示例医院的重点专科为呼吸内科。'))
    assert result['status']=='ok' and len(calls['search'])==2
    assert calls['search'][0]['query']!=calls['search'][1]['query']


def test_unreadable_result_can_switch_configured_provider(monkeypatch):
    def search(args,n):
        if n==2: assert args['provider']=='baidu'
        return {'ok':True,'candidates':[candidate(str(n))],
                'provider_attempts':[{'provider':'bocha' if n==1 else 'baidu','candidate_count':1}]}
    result,calls=lookup(monkeypatch,'示例医院科室',search,
        lambda args,n:{'ok':False,'error':'page_http_error','http_status':404} if n==1 else page('示例医院设内科。'),
        providers=['bocha','baidu'])
    assert result['sources'] and len(calls['search'])==2


def test_browser_fills_missing_body_and_keeps_direct_evidence(monkeypatch):
    result,calls=lookup(monkeypatch,'示例医院科室',lambda args,n:{'ok':True,'candidates':[candidate()]},
        lambda args,n:page('示例医院欢迎您。','direct') if args['method']=='direct' else page('示例医院设呼吸内科。','browser'),browser=True)
    assert result['status']=='ok' and {s['id'] for s in result['sources']}=={'direct','browser'}
    assert [r['method'] for r in calls['read']]==['direct','browser']


def test_partial_answer_survives_later_failures_and_retry_focuses_missing_topic(monkeypatch):
    result,calls=lookup(monkeypatch,'示例医院重点科室和地址',lambda args,n:
        {'ok':True,'candidates':[candidate()]} if n==1 else {'ok':False,'retryable':True,'candidates':[],'error':'search_timeout'},
        lambda args,n:page('示例医院地址：示例路1号。'))
    assert result['status']=='partial' and result['sources']
    assert 'specialty' in result['missing_topics']
    assert len(calls['search'])==3 and len(calls['read'])==1
    assert '重点' in calls['search'][1]['query'] and '地址' not in calls['search'][1]['query']


def test_permanent_provider_failure_does_not_repeat_with_other_words(monkeypatch):
    result,calls=lookup(monkeypatch,'广东省有哪些医院',lambda args,n:
        {'ok':False,'candidates':[],'retryable':False,'error':'all_search_providers_failed'})
    assert result['status']=='service_unavailable' and len(calls['search'])==1 and not calls['read']
    assert '服务' in hospital_failure('广东省有哪些医院',result)


@pytest.mark.parametrize('has_candidates,expected',[(False,'no_results'),(True,'unreadable')])
def test_no_match_and_read_failure_have_different_outcomes(monkeypatch,has_candidates,expected):
    result,calls=lookup(monkeypatch,'广东省有哪些医院',lambda args,n:
        {'ok':True,'candidates':[candidate(title='广东医院目录')] if has_candidates else []},
        lambda args,n:{'ok':False,'error':'page_http_error','http_status':404},browser=True)
    assert result['status']==expected and not result['sources']
    assert len(calls['search'])<=3 and result['browser_reads']==0


def test_wrong_hospital_body_is_not_used(monkeypatch):
    result,calls=lookup(monkeypatch,'示例医院地址',lambda args,n:{'ok':True,'candidates':[candidate()]},
                        lambda args,n:page('另一家医院地址：其他路2号。'))
    assert result['sources']==[] and result['status']=='insufficient_evidence'
    assert len(calls['read'])==1


def test_queries_exclude_patient_history_and_identifiers(monkeypatch):
    result,calls=lookup(monkeypatch,'我爸张三在普宁康美医院，患者张三68岁，血压180，联系电话13812345678，想知道医院科室',
                        lambda args,n:{'ok':True,'candidates':[]})
    text=str(calls['search'])
    for private in ['张三','68岁','180','13812345678','我爸','患者']:
        assert private not in text
    assert '普宁康美医院' in text


def test_hospital_context_continues_and_changes_region_without_private_context():
    previous=[{'role':'assistant','result':{'kind':'chat','intent':'hospital','hospital_context':{
        'kind':'named','names':['中山大学附属第一医院'],'region':'','topics':['address']}}}]
    plan=plan_hospital('重点科室呢',previous)
    assert plan.names==['中山大学附属第一医院'] and plan.region=='' and 'specialty' in plan.topics
    assert fallback_intent('重点科室呢',previous).response_mode=='hospital'
    assert plan_hospital('深圳呢',previous).kind=='directory'
    assert plan_hospital('深圳院区呢',previous).names==['中山大学附属第一医院']
    previous.append({'role':'assistant','result':{'kind':'chat','intent':'daily','content':'普通问题'}})
    assert plan_hospital('它在哪里',previous).kind=='clarify'
    assert fallback_intent('CT是什么',previous).response_mode=='medical_education'


def test_list_followup_uses_only_names_grounded_in_last_answer():
    class Client:
        async def complete(self,messages):
            return {'content':'部分医院包括 **广东省人民医院**、**虚构医院**。'}
    source={'id':'one','title':'广东医院名录','text':'广东省人民医院位于广州。','url':'https://example.com','kind':'public'}
    result=asyncio.run(chat(Repo(),Client(),True,'run','广东有哪些医院',[],False,intent='hospital',sources=[source]))
    assert result['hospital_context']['listed_names']==['广东省人民医院']
    plan=plan_hospital('它的地址呢',[{'role':'assistant','result':result}])
    assert plan.kind=='named' and plan.names==['广东省人民医院']


def test_user_can_decline_external_search(monkeypatch):
    def unexpected(*args): raise AssertionError('search must not be invoked')
    result,calls=lookup(monkeypatch,'不要联网，告诉我广东有哪些医院',unexpected)
    assert result['status']=='not_requested' and calls['search']==[]
    assert '没有发起外部查询' in hospital_failure('不要联网，告诉我广东有哪些医院',result)


def test_timeout_preserves_fast_page_while_another_page_is_pending(monkeypatch):
    async def reader(args,n):
        if args['candidate_id']=='slow': await asyncio.sleep(3)
        return page('示例医院已读取的介绍。',args['candidate_id'])
    result,calls=lookup(monkeypatch,'示例医院重点科室',lambda args,n:{'ok':True,'candidates':[candidate('fast'),candidate('slow')]},
                        reader,settings=Settings(mode='demo',external_enabled=True,hospital_lookup_timeout=1))
    assert result['status']=='partial' and result['sources'][0]['id']=='fast'
    assert result['sources'][0]['text']=='示例医院已读取的介绍。'


def test_search_and_browser_work_remain_within_deployment_budgets(monkeypatch):
    result,calls=lookup(monkeypatch,'示例医院重点科室',lambda args,n:{'ok':True,'candidates':[candidate(str(n)+'-'+str(i)) for i in range(12)]},
                        lambda args,n:{'ok':False,'error':'page_network_error'},browser=True)
    assert len(calls['search'])<=3
    assert len([r for r in calls['read'] if r['method']=='direct'])<=8
    assert len([r for r in calls['read'] if r['method']=='browser'])<=2


def test_directory_title_and_document_viewer_navigation_are_not_evidence(monkeypatch):
    result,calls=lookup(monkeypatch,'广东省有哪些医院',lambda args,n:{'ok':True,'candidates':[candidate(str(n),title='广东省医院大全')]},
        lambda args,n:page('首页 下载 积分 广东省医院大全 全文阅读已结束。',str(n),title='广东省医院大全'),browser=True)
    assert not result['sources'] and result['search_rounds']==3
    assert result['browser_reads']==2


def test_empty_round_followed_by_service_failures_is_not_reported_as_no_results(monkeypatch):
    result,_=lookup(monkeypatch,'广东省有哪些医院',lambda args,n:
        {'ok':True,'candidates':[]} if n==1 else {'ok':False,'retryable':True,'error':'search_timeout'})
    assert result['status']=='service_unavailable' and result['search_failures']==2


def test_city_only_followup_routes_to_a_new_directory_not_last_hospital():
    history=[{'role':'assistant','result':{'intent':'hospital','hospital_context':{
        'kind':'named','region':'深圳','names':['北京大学深圳医院'],'topics':['doctors']}}}]
    assert fallback_intent('深圳',history).response_mode=='hospital'
    plan=plan_hospital('深圳',history)
    assert plan.kind=='directory' and not plan.names and not plan.topics
    assert all('北京大学' not in query and '医生' not in query for query in plan.queries())
    history[0]['result']['hospital_context']={'kind':'directory','region':'广东省','topics':['level'],'public_terms':'三级甲等'}
    assert '三级甲等' in plan_hospital('深圳',history).queries()[0]


def test_directory_expands_after_single_hospital_and_uses_multiple_read_bodies(monkeypatch):
    names=['深圳市合成一医院','深圳市合成二医院','深圳市合成三医院','深圳市合成四医院','深圳市合成五医院','深圳市合成六医院']
    result,calls=lookup(monkeypatch,'深圳有哪些医院',lambda args,n:{'ok':True,'candidates':[
        candidate(str(n),title='深圳医院名单')]},lambda args,n:
        page(names[0]+'。','first',title='深圳医院介绍') if n==1 else page('、'.join(names)+'。','list',title='深圳医疗机构名录'))
    assert len(calls['search'])==3 and result['directory_entry_count']==6
    assert len(result['sources'])==2 and not result['directory_coverage_low']
    assert '名录' in calls['search'][1]['query']


def test_duplicate_hospital_across_pages_does_not_satisfy_directory_coverage(monkeypatch):
    result,calls=lookup(monkeypatch,'深圳有哪些医院',lambda args,n:{'ok':True,'candidates':[
        candidate(str(n),title='深圳医院名单')]},lambda args,n:page('北京大学深圳医院。',str(n),title='深圳医院介绍'))
    assert len(calls['search'])==3 and result['directory_entry_count']==1
    assert result['directory_coverage_low'] and result['status']=='partial'


def test_directory_budget_leaves_room_for_third_query(monkeypatch):
    result,calls=lookup(monkeypatch,'深圳有哪些医院',lambda args,n:{'ok':True,'candidates':[
        candidate(str(n)+'-'+str(i),title='深圳医院名单') for i in range(12)]},
        lambda args,n:{'ok':False,'error':'page_http_error','http_status':404})
    assert len(calls['search'])==3 and result['direct_reads']==8


def test_directory_form_filters_are_not_counted_as_hospitals(monkeypatch):
    result,_=lookup(monkeypatch,'深圳有哪些医院',lambda args,n:{'ok':True,'candidates':[
        candidate(str(n),title='深圳医院名单')]},lambda args,n:page(
            '是否医保定点医院\n综合医院\n专科医院\n公立医院\n民营医院\n其他医院\n北京大学深圳医院。',str(n),title='深圳医院名录'))
    assert result['directory_entry_count']==1 and result['search_rounds']==3
