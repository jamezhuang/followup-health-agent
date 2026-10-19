import asyncio
from app.followup.material_status import mark_material_status, NOTICE
from app.followup.hospital_planner import plan_hospital
from app.followup.intent import analyze_intent, IntentDecision
from app.followup.chat import chat
from app.followup.evidence_selection import select_task_sources
from app.followup.verification_policy import registered_website_answer


class Repo:
    def stage(self,*args,**kwargs):pass


def test_confirmation_notice_is_server_generated_for_both_result_shapes():
    documents=[{'id':'pending','confirmed':False},{'id':'confirmed','confirmed':True}]
    for result in [{'kind':'chat','content':'报告回答'},{'kind':'followup','claims':[]}]:
        mark_material_status(result,documents)
        assert result['material_notice']==NOTICE
        assert result['material_status']['document_ids']==['pending']
        if result['kind']=='chat':
            assert result['content'].startswith(NOTICE)
            mark_material_status(result,documents)
            assert result['content'].count(NOTICE)==1
    result={'kind':'chat','content':'已核对资料回答'}
    mark_material_status(result,[documents[1]])
    assert 'material_notice' not in result


def test_request_prefixes_do_not_become_institution_names():
    for prefix in ['给','给我','发我','提供','请给我']:
        plan=plan_hospital(prefix+'清禾医院地址、交通和明天实时号源')
        assert plan.names==['清禾医院']


def test_hospital_clarification_names_the_missing_institution():
    history=[{'role':'assistant','result':{'kind':'chat','intent':'daily','consultation_type':'hospital',
        'content':'两家医院','hospital_context':{'kind':'named','names':['清禾医院','明川医院'],'topics':['address']}}}]
    result=asyncio.run(chat(Repo(),None,False,'r','给我他的官网',history,False,intent='hospital',clarify=True))
    assert '清禾医院' in result['content'] and '明川医院' in result['content']
    assert '解读报告' not in result['content']


def test_preparation_does_not_turn_into_an_implicit_mutation():
    class Client:
        async def structured(self,*args):return IntentDecision(intent='followup',operation='update',reason_code='personal_followup')
    decision=asyncio.run(analyze_intent(Repo(),Client(),True,'r','整理复诊材料，把以前备注的头晕问题加进去',[],True))
    assert decision.operation=='prepare'


def test_task_selection_keeps_different_campus_evidence_within_budget():
    sources=[{'id':'north','title':'清禾医院北院区地址','text':'北院区位于合成市清禾路10号。'},
        {'id':'other','title':'明川医院地址','text':'明川医院位于明川路30号。'},
        {'id':'directory','title':'清禾医院介绍','text':'清禾医院有北院区和南院区。'},
        {'id':'south','title':'清禾医院南院区地址','text':'南院区位于合成市新桥路20号。'}]
    tasks=[{'query':'清禾医院北院区地址'},{'query':'清禾医院南院区地址'},{'query':'明川医院地址'}]
    selected=select_task_sources(tasks,sources,3)
    assert {source['id'] for source in selected}=={'north','south','other'}


def test_deterministic_path_requires_exact_registered_website_and_narrow_request():
    lookup={'context':{'names':['南方医科大学珠江医院'],'topics':['website']},'sources':[
        {'url':'https://www.zjyy.com.cn/','authority':'registered','retrieval_origin':'verified_registry'}]}
    assert registered_website_answer('珠江医院官网是什么',lookup)
    assert registered_website_answer('官网安全吗',lookup) is None
    assert registered_website_answer('怎么在官网挂号',lookup) is None
    lookup['sources'][0]['url']='https://www.sshospital.cn/'
    assert registered_website_answer('珠江医院官网是什么',lookup) is None


def test_verified_entry_avoids_both_generation_and_review_calls():
    class Client:
        async def complete(self,*args):raise AssertionError('No model call required')
        async def structured(self,*args):raise AssertionError('No model call required')
    source={'id':'registry:zjyy.com.cn','title':'南方医科大学珠江医院已登记入口',
        'text':'已核对官网入口','url':'https://www.zjyy.com.cn/','authority':'registered','retrieval_origin':'verified_registry'}
    lookup={'context':{'kind':'named','names':['南方医科大学珠江医院'],'topics':['website']},'sources':[source],'status':'ok'}
    result=asyncio.run(chat(Repo(),Client(),True,'r','南方医科大学珠江医院官网',[],False,intent='hospital',sources=[source],hospital_search=lookup))
    assert 'zjyy.com.cn' in result['content'] and result['answer_review']['status']=='deterministic_source_check'
