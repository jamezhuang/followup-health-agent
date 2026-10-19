import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.followup.hospital import hospital_sources, _usable, _related_candidate
from app.followup.hospital_evidence import institution_groups, qualifiers_match
from app.followup.hospital_planner import plan_hospital
from app.followup.rag_pipeline import match_hospital_sources, supported_hospital_answer, source_packet


class Repo:
    def stage(self,*args,**kwargs):pass


def page(identifier,title,text,host='hospital.example'):
    return {'id':identifier,'title':title,'text':text,'url':f'https://{host}/{identifier}'}


def evaluate(plan,sources,match):
    class Client:
        async def structured(self,schema,prompt,data):
            assert data['institution_groups']
            return schema(matches=[match])
    return asyncio.run(match_hospital_sources(Client(),Repo(),'r',plan,sources))


def test_same_hospital_pages_jointly_support_region_service_and_surgical_synonym():
    plan=plan_hospital('珠三角地区有哪些医院治疗韧带断裂手术')
    sources=[page('address','合成大学甲医院 联系我们','医院地址：广州市。'),
             page('service','合成大学甲医院运动医学科','开展关节镜下韧带重建。')]
    accepted=evaluate(plan,sources,{'source_id':'service','evidence_source_ids':['address'],
        'institution':'合成大学甲医院','region_quote':'广州市','focus_quote':'韧带重建',
        'treatment_quote':'关节镜下韧带重建'})
    assert {item['id'] for item in accepted}=={'address','service'}
    assert all(_usable(plan,item) for item in accepted)
    assert all(item['evidence_level']=='confirmed_service' for item in accepted)
    assert accepted[0]['text'] in {source['text'] for source in sources}
    assert source_packet(accepted)[0]['evidence_source_ids']


def test_regional_group_is_not_mistaken_for_a_district_because_it_ends_with_qu():
    from app.followup.hospital_policy import directory_policy
    goal='珠三角地区有哪些医院治疗韧带断裂手术'
    policy=directory_policy(plan_hospital(goal),goal)
    assert policy['scope']=='regional_group' and policy['target_min']==8
    assert policy['diversify_regions']


@pytest.mark.parametrize('change',['other_hospital','other_domain','invented_quote','wrong_city','unknown_source'])
def test_assembly_rejects_mixed_identity_or_unsupported_claims(change):
    plan=plan_hospital('深圳有哪些医院治疗韧带断裂手术')
    sources=[page('address','合成大学甲医院 联系我们','医院地址：深圳市。'),
             page('service','合成大学甲医院运动医学科','开展关节镜下韧带重建。')]
    match={'source_id':'service','evidence_source_ids':['address'],'institution':'合成大学甲医院',
           'region_quote':'深圳市','focus_quote':'韧带重建','treatment_quote':'关节镜下韧带重建'}
    if change=='other_hospital':sources[0]['title']='合成大学乙医院 联系我们'
    if change=='other_domain':sources[0]['url']='https://other.example/address'
    if change=='invented_quote':match['focus_quote']='前交叉韧带手术治愈率最高'
    if change=='wrong_city':sources[0]['text']='医院地址：广州市。';match['region_quote']='广州市'
    if change=='unknown_source':match['evidence_source_ids']=['missing']
    assert evaluate(plan,sources,match)==[]


def test_qualifiers_no_longer_disable_semantic_matching_but_still_require_evidence():
    plan=plan_hospital('广州有哪些三甲医院治疗韧带断裂手术')
    source=page('s','广州合成大学甲医院','本院为三级甲等医院，开展关节镜下韧带重建。')
    match={'source_id':'s','institution':'广州合成大学甲医院','region_quote':'广州',
        'focus_quote':'韧带重建','treatment_quote':'关节镜下韧带重建',
        'qualifier_quotes':['本院为三级甲等医院']}
    assert evaluate(plan,[source],match)
    assert evaluate(plan,[source],{**match,'qualifier_quotes':[]})==[]
    assert evaluate(plan,[source],{**match,'qualifier_quotes':['二级甲等医院']})==[]


def test_semantic_matching_cannot_override_a_known_province_conflict_and_nationwide_is_valid():
    source=page('s','广州合成大学甲医院运动医学科','广州开展韧带重建。')
    match={'source_id':'s','institution':'广州合成大学甲医院','region_quote':'广州',
           'focus_quote':'韧带重建','treatment_quote':'韧带重建'}
    assert evaluate(plan_hospital('湖南省有哪些医院治疗韧带断裂手术'),[source],match)==[]
    assert evaluate(plan_hospital('全国有哪些医院治疗韧带断裂手术'),[source],match)


def test_related_department_is_retained_but_never_promoted_by_missing_service_evidence():
    plan=plan_hospital('深圳有哪些医院治疗韧带断裂手术')
    source=page('s','深圳合成大学甲医院运动医学科','本院设有运动医学科。')
    candidate=_related_candidate(plan,source)
    assert candidate and _usable(plan,candidate)
    assert candidate['evidence_level']=='related_department'
    assert _related_candidate(plan,{**source,'text':'本院未设运动医学科。'}) is None
    answer=supported_hospital_answer('深圳有哪些医院治疗韧带断裂手术',[candidate])
    assert '尚未核实' in answer
    assert not _usable(plan,source)
    alias=page('alias','深圳合成大学乙医院骨伤外科','科室介绍。')
    assert _related_candidate(plan,alias)['evidence_level']=='related_department'


def test_department_only_results_can_cover_four_institutions_without_false_surgery_claims(monkeypatch):
    import app.web_tools as tools_module
    names=[f'广州合成大学{char}医院' for char in '甲乙丙丁']
    calls=[]
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False)
        async def search(self,args):
            calls.append(('search',args['query']))
            return {'ok':True,'candidates':[{'candidate_id':name,'title':name+'运动医学科',
                    'snippet':'广州科室介绍','url':'https://hospital.example/'+name} for name in names]}
        async def read(self,args):
            calls.append(('read',args['candidate_id']))
            return {'ok':True,'sources':[page(args['candidate_id'],args['candidate_id']+'运动医学科','本院设有运动医学科。')]}
    monkeypatch.setattr(tools_module,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True),Repo(),'r',
        '珠三角地区有哪些医院擅长处理韧带断裂手术',return_details=True))
    assert result['directory_entry_count']==4
    assert result['retrieval_diagnostics']['related_institutions']==4
    assert result['retrieval_diagnostics']['confirmed_institutions']==0
    assert all(source['evidence_level']=='related_department' for source in result['sources'])
    assert result['direct_reads']<=16 and result['search_rounds']<=3
    assert len([call for call in calls if call[0]=='read'])==4


def test_unknown_institution_alias_and_directory_do_not_join_pages():
    sources=[page('a','合成大学甲医院','深圳市'),page('b','合成甲医院','韧带重建'),
             page('c','医院名单','合成大学甲医院、合成甲医院')]
    groups=institution_groups(sources)
    assert all(not {'a','b'}.issubset(group['source_ids']) for group in groups)
    assert all('c' not in group['source_ids'] for group in groups)


def test_later_address_pages_complete_earlier_service_pages_instead_of_losing_them(monkeypatch):
    import app.web_tools as tools_module
    from app.followup.rag_pipeline import QueryRewrite, HospitalMatches, EvidenceReview
    names=[f'合成大学{char}医院' for char in '甲乙丙丁']
    rows={}
    for index,name in enumerate(names):
        rows[name+'service']=page(name+'service',name+'运动医学科','开展关节镜下韧带重建。',f'h{index}.example')
        rows[name+'address']=page(name+'address',name+' 联系我们','本院地址：广州市。',f'h{index}.example')
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False);self.round=0
        async def search(self,args):
            if any(name in args['query'] for name in names):return {'ok':True,'candidates':[]}
            self.round+=1
            identifiers=[name+('service' if self.round==1 else 'address') for name in names]
            return {'ok':True,'candidates':[{'candidate_id':key,'title':rows[key]['title'],
                'snippet':'广州韧带诊疗','url':rows[key]['url']} for key in identifiers]}
        async def read(self,args):return {'ok':True,'sources':[rows[args['candidate_id']]]}
    class Client:
        async def structured(self,schema,prompt,data):
            if schema is QueryRewrite:return schema(queries=data['existing_queries'])
            if schema is EvidenceReview:return schema(sufficient=False,source_ids=[],missing=['quantity'])
            assert schema is HospitalMatches
            matches=[]
            for group in data['institution_groups']:
                if len(group['source_ids'])<2:continue
                matches.append({'source_id':group['source_ids'][0],'evidence_source_ids':group['source_ids'][1:],
                    'institution':group['institution'],'region_quote':'广州市','focus_quote':'韧带重建',
                    'treatment_quote':'关节镜下韧带重建'})
            return schema(matches=matches)
    monkeypatch.setattr(tools_module,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='live',external_enabled=True),Repo(),'r',
        '珠三角地区有哪些医院擅长处理韧带断裂手术',return_details=True,client=Client()))
    assert result['directory_entry_count']==4
    assert result['retrieval_diagnostics']['confirmed_institutions']==4
    assert {source['id'] for source in result['sources']}==set(rows)
    assert all(source['evidence_level']=='confirmed_service' for source in result['sources'])


def test_explicit_exclusion_requires_positive_support_and_cached_scope_cannot_change_institution():
    assert qualifiers_match(['非民营'],'本院是公立医院')
    assert not qualifiers_match(['非民营'],'医院开展诊疗')
    assert not qualifiers_match(['非民营'],'本院是民营医院')
    plan=plan_hospital('深圳有哪些医院治疗韧带断裂手术')
    candidate=_related_candidate(plan,page('s','深圳合成大学甲医院运动医学科','本院设有运动医学科。'))
    named=plan_hospital('深圳合成大学乙医院可以治疗韧带断裂手术吗')
    assert not _usable(named,candidate)
