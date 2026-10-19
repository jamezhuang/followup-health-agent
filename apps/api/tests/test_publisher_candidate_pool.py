import asyncio
from app.config import Settings
from app.web_access import SourcePolicy
from app.web_providers import WebProviders,prioritize_publisher_candidates

def test_unregistered_publisher_is_ranked_before_candidate_truncation():
    candidates=[{'url':f'https://platform{i}.example/hospital/address','title':'远方大学附属医院地址电话交通','authority':'unverified'} for i in range(49)]
    candidates.append({'url':'https://new-hospital.example/contact','title':'联系我们-远方大学附属医院','authority':'unverified'})
    class Providers(WebProviders):
        def available(self):return ['bocha']
        def configured(self,name):return name=='bocha'
        async def one_search(self,*args):return candidates,{'provider':'bocha','raw_count':50,'candidate_count':50}
    settings=Settings()
    result=asyncio.run(Providers(settings,SourcePolicy(settings)).search('远方大学附属医院 官方网站',12,'bocha','public'))
    assert len(result['candidates'])==12
    assert result['candidates'][0]['url']=='https://new-hospital.example/contact'
    assert result['candidates'][0]['authority']=='unverified'
    from app.followup.hospital import _candidate_score
    from app.followup.hospital_planner import plan_hospital
    plan=plan_hospital('远方大学附属医院地址')
    assert _candidate_score(plan,result['candidates'][0])>_candidate_score(plan,candidates[0])
    assert len(candidates)==50 and candidates[-1]['url']=='https://new-hospital.example/contact'

def test_discovery_preference_does_not_change_nonwebsite_query_or_source_trust():
    rows=[{'url':'https://profile.example/','title':'远方大学附属医院','authority':'unverified'},
          {'url':'https://gov.example.gov.cn/info','title':'医疗机构目录','authority':'government'}]
    assert prioritize_publisher_candidates('远方大学附属医院地址',rows)==rows
    ranked=prioritize_publisher_candidates('远方大学附属医院官网',rows)
    assert ranked[0]['authority']=='government'
    assert rows[0]['authority']=='unverified'
