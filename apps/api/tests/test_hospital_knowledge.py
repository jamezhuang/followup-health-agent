import asyncio

import pytest

from app.followup.hospital_knowledge import clinical_hints, service_categories, SERVICE_CATEGORIES
from app.followup.hospital_planner import plan_hospital
from app.followup.hospital import _relevant, _usable
from app.followup.hospital_templates import answer_frame
from app.followup.intent import fallback_intent
from app.followup.rag_pipeline import rewrite_hospital


@pytest.mark.parametrize('focus,dept', [
    ('韧带断裂','运动医学科'), ('膝关节韧带撕裂','骨科'), ('踝关节韧带拉伤','运动医学科'),
    ('前交叉韧带断裂','运动医学科'), ('后交叉韧带损伤','骨科'),
    ('半月板撕裂','运动医学科'), ('肩袖撕裂','骨科'), ('跟腱断裂','骨科'),
    ('股骨骨折','骨科'), ('腰椎间盘突出','脊柱外科'), ('类风湿关节炎','风湿免疫科'),
    ('乳腺癌','乳腺外科'), ('肺癌','胸外科'), ('胃癌','胃肠外科'),
    ('前列腺癌','泌尿外科'), ('白血病','血液内科'), ('甲状腺结节','内分泌科'),
    ('糖尿病','内分泌科'), ('冠心病','心血管内科'), ('哮喘','呼吸内科'),
    ('脑梗死','神经内科'), ('肾结石','泌尿外科'), ('胆囊结石','肝胆外科'),
    ('白内障','眼科'), ('鼻窦炎','耳鼻喉科'), ('银屑病','皮肤科'), ('不孕症','生殖医学科'),
])
def test_explicit_clinical_focus_survives_without_a_model(focus, dept):
    plan=plan_hospital(f'湖南省有哪些医院治疗{focus}')
    assert plan.care_topic==focus
    assert dept in plan.related_departments
    assert focus in plan.queries()[0]
    assert all('湖南省' in query for query in plan.queries())
    assert dept in plan.queries()[-1]


def test_torn_ligament_keeps_structure_and_treatment_and_does_not_prove_expertise():
    plan=plan_hospital('珠三角地区有哪些医院擅长处理韧带断裂手术')
    assert plan.care_topic=='韧带断裂' and plan.treatment=='手术'
    assert '韧带损伤' in plan.focus_variants
    assert '韧带断裂' in plan.queries()[0]
    assert any('运动医学科' in query and '韧带损伤' in query for query in plan.queries())
    assert _relevant(plan,'广州合成医院')
    assert _relevant(plan,'深圳合成医院')
    assert not _relevant(plan,'长沙合成医院')
    assert not _relevant(plan,'香港合成医院')
    assert not _usable(plan,{'title':'广州合成医院骨科','text':'广州合成医院设有骨科。'})
    _,_,variants=clinical_hints('前交叉韧带断裂')
    assert all('前交叉韧带' in value for value in variants)


@pytest.mark.parametrize('symptom',['头疼','胃痛','关节痛','膝盖疼','头晕','胸闷'])
def test_symptoms_do_not_become_diseases(symptom):
    focus,depts,variants=clinical_hints(symptom)
    assert not focus and not depts and not variants


@pytest.mark.parametrize('category,question', [
    ('before_visit','怎么预约挂号'), ('visit_process','过号了怎么重新报到'),
    ('insurance_cost','异地医保怎么报销'), ('inpatient','陪护和探视怎么安排'),
    ('department_doctor','怎么查医生专长和多学科会诊'),
    ('records_certificate','病案复印需要哪些代办材料'),
    ('checkup','体检套餐怎么预约'), ('convenience','轮椅在哪里租借'),
    ('medical_questions','这个病会传染或遗传吗'),
])
def test_all_handbook_categories_reach_answer_frame(category,question):
    goal='广东省人民医院'+question
    plan=plan_hospital(goal)
    assert category in service_categories(goal)
    assert category in plan.service_categories
    _, frame=answer_frame(goal,plan.context())
    assert SERVICE_CATEGORIES[category]['label'] in frame


@pytest.mark.parametrize('question,expected', [
    ('候诊过号了怎么办','hospital'), ('病假单怎么办理','hospital'),
    ('体检套餐在哪里预约','hospital'), ('病历复印怎么代办','hospital'),
    ('化疗有哪些副作用','medical_education'), ('癌症会遗传吗','medical_education'),
    ('抽血检查需要空腹吗','medical_education'),
    ('韧带断裂是什么意思','medical_education'), ('膝盖疼挂哪个科','medical_education'),
])
def test_service_routes_do_not_swallow_medical_education(question,expected):
    result=fallback_intent(question,[])
    assert result.intent=='daily' and result.consultation_type==expected


def test_hints_survive_empty_model_rewrite():
    class Repo:
        def stage(self,*args,**kwargs):pass
    class Client:
        async def structured(self,schema,*args):
            return schema(queries=['珠三角地区 医院名单'])
    plan=plan_hospital('珠三角地区有哪些医院擅长处理韧带断裂手术')
    queries=asyncio.run(rewrite_hospital(Client(),Repo(),'r',plan,None))
    assert plan.related_departments==['运动医学科','骨科']
    assert plan.focus_variants==['韧带损伤']
    assert '韧带断裂' in queries[0] and '手术' in queries[0]
