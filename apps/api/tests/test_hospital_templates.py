import asyncio

import pytest

from app.followup.hospital_planner import plan_hospital
from app.followup.hospital_policy import directory_policy
from app.followup.hospital_templates import answer_frame
from app.followup.hospital import _usable, _candidate_score
from app.followup.chat import chat


@pytest.mark.parametrize('goal,expected',[
    ('东莞有哪些医院','directory'),
    ('广州有哪些医院擅长处理乳腺癌','disease_choice'),
    ('深圳有哪些呼吸内科医院','disease_choice'),
    ('广东省人民医院有哪些重点科室','specialty'),
    ('广东省人民医院地址在哪里','address'),
    ('广东省人民医院怎么预约挂号','appointment'),
    ('广东省人民医院异地医保怎么报销','cost'),
    ('广东省医院总数一共有多少','count'),
    ('广东省人民医院和中山大学附属第一医院怎么选','comparison'),
])
def test_frame_matches_the_actual_question(goal,expected):
    plan=plan_hospital(goal)
    policy=directory_policy(plan,goal) if plan.kind=='directory' else None
    style,frame=answer_frame(goal,plan.context(),policy)
    assert style==expected,(plan,style)
    assert frame


@pytest.mark.parametrize('topic',['乳腺癌','肺结节','糖尿病','高血压','哮喘'])
def test_search_preserves_public_care_topic_and_drops_patient_details(topic):
    goal=f'我妈妈68岁，姓名测试甲，病历号123456789，广州有哪些医院擅长治疗{topic}'
    plan=plan_hospital(goal)
    assert plan.care_topic==topic
    assert topic in plan.queries(directory_policy(plan,goal))[0]
    for query in plan.queries(directory_policy(plan,goal)):
        assert '广州' in query
        assert all(private not in query for private in ['妈妈','68','测试甲','123456789'])


def test_care_topic_survives_city_followup_but_not_a_new_general_directory():
    first=plan_hospital('广州有哪些医院擅长治疗乳腺癌')
    history=[{'role':'assistant','result':{'intent':'hospital','hospital_context':first.context()}}]
    assert plan_hospital('东莞',history).care_topic=='乳腺癌'
    assert plan_hospital('东莞有哪些医院',history).care_topic==''


@pytest.mark.parametrize('goal,department',[
    ('深圳有哪些呼吸内科医院','呼吸内科'),
    ('东莞哪些医院乳腺科比较好','乳腺科'),
    ('广州有哪些医院有康复医学科','康复医学科'),
])
def test_department_search_uses_the_care_category(goal,department):
    plan=plan_hospital(goal)
    assert plan.department==department
    assert all(department in query for query in plan.queries(directory_policy(plan,goal)))


def test_disease_choice_does_not_use_generic_list_as_specialty_evidence():
    plan=plan_hospital('广州有哪些医院擅长治疗乳腺癌')
    generic={'title':'广州医院名单','text':'广州：测试甲医院、测试乙医院。'}
    specific={'title':'乳腺科介绍','text':'广州测试甲医院提供乳腺癌诊疗服务。'}
    assert not _usable(plan,generic,directory_policy(plan,'广州有哪些医院擅长治疗乳腺癌'))
    assert _usable(plan,specific,directory_policy(plan,'广州有哪些医院擅长治疗乳腺癌'))


def test_limited_purpose_roster_is_not_preferred_over_general_directory():
    plan=plan_hospital('东莞有哪些医院')
    broad={'title':'东莞医院大全','url':'https://example.com/list'}
    narrow={'title':'东莞公共场所从业人员健康检查机构名单','url':'https://example.gov.cn/list'}
    assert _candidate_score(plan,broad)>_candidate_score(plan,narrow)


def test_live_generation_receives_selected_frame_without_changing_user_goal():
    class Repo:
        def stage(self,*a,**kw):pass
    class Client:
        async def complete(self,messages):
            assert '【按疾病/诊疗方向选择医院】' in messages[0]['content']
            assert '相关科室' in messages[0]['content']
            assert '不能每轮套同一个长模板' in messages[0]['content']
            assert messages[-1]['content']==goal
            return {'content':'### 广州\n1. **测试甲医院**\n   - **相关科室**：乳腺科。'}
    goal='广州有哪些医院擅长治疗乳腺癌'
    plan=plan_hospital(goal)
    result=asyncio.run(chat(Repo(),Client(),True,'run',goal,[],False,intent='hospital',
        sources=[{'id':'s','title':'医院介绍','text':'广州测试甲医院乳腺科提供乳腺癌诊疗服务。'}],
        hospital_search={'context':plan.context(),'answer_policy':directory_policy(plan,goal),'directory_entry_count':1}))
    assert '### 广州' in result['content']


def test_single_bounded_correction_can_remove_excess_hospitals():
    names=[f'合成{number}医院' for number in '甲乙丙丁戊己庚辛壬癸']
    class Repo:
        def stage(self,*a,**kw):pass
    class Client:
        def __init__(self):self.calls=0
        async def complete(self,messages):
            self.calls+=1
            chosen=names if self.calls==1 else names[:8]
            return {'content':'\n'.join(f'{i}. **{name}**' for i,name in enumerate(chosen,1))}
    client=Client();goal='广州有哪些医院';plan=plan_hospital(goal)
    result=asyncio.run(chat(Repo(),client,True,'run',goal,[],False,intent='hospital',
        sources=[{'id':'s','title':'广州医院名单','text':'广州：'+'、'.join(names)}],hospital_search={
            'context':plan.context(),'answer_policy':directory_policy(plan,goal),'directory_entry_count':10}))
    assert client.calls==2 and result['directory_answer_count']==8
