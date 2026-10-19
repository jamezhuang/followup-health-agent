import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.followup.hospital import hospital_sources,_usable,_directory_names,_discovery_only
from app.followup.hospital_planner import plan_hospital
from app.followup.hospital_policy import directory_policy
from app.followup.chat import grounded_hospital_names


class Repo:
    def stage(self,*a,**kw):pass


def test_actual_page_title_is_evidence_for_institution_name():
    plan=plan_hospital('有哪些医院擅长乳腺癌')
    source={'title':'合成大学附属第一医院_乳腺外科','text':'本院开展乳腺癌诊疗服务。'}
    assert _usable(plan,source,directory_policy(plan,'有哪些医院擅长乳腺癌'))
    assert grounded_hospital_names('**合成大学附属第一医院**',[source])==['合成大学附属第一医院']


def test_clinical_profile_does_not_count_every_hospital_in_doctor_biography():
    source={'title':'合成大学附属第一医院','text':'曾赴合作医院学习。',
            'evidence_institutions':['合成大学附属第一医院']}
    assert _directory_names(source)=={'合成大学附属第一医院'}
    assert _discovery_only({'title':'乳腺癌专家排名_最好的专家'})


def test_all_rewrites_keep_page_budget_and_profiles_do_not_map_site_navigation(monkeypatch):
    import app.web_tools as module
    calls=[]
    class Tools:
        def __init__(self,*a):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False);self.round=0
        async def search(self,args):
            self.round+=1;calls.append(('search',self.round))
            return {'ok':True,'candidates':[{'candidate_id':f'{self.round}-{n}',
                'title':'合成医院乳腺癌门诊','url':f'https://example.com/{self.round}-{n}'} for n in range(6)]}
        async def read(self,args):
            calls.append(('read',args['candidate_id']))
            if args['candidate_id']=='1-0':
                return {'ok':True,'sources':[{'id':'s','title':'合成医院乳腺门诊','text':'合成医院提供乳腺癌诊疗服务。'}]}
            return {'ok':False,'error':'page_http_error','http_status':403}
        async def map(self,*a):raise AssertionError('individual profiles must not map unrelated navigation')
    monkeypatch.setattr(module,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True,hospital_service_page_limit=8),Repo(),'r',
        '有哪些医院比较擅长处理乳腺癌',return_details=True))
    assert result['search_rounds']==3 and result['direct_reads']==8
    assert result['link_maps']==0 and len(result['sources'])==1
    assert [entry for entry in calls if entry[0]=='search']==[('search',1),('search',2),('search',3)]


def test_discovered_institutions_are_verified_separately_and_rankings_are_not_facts(monkeypatch):
    import app.web_tools as module
    names=['合成大学甲医院','合成大学乙医院','合成大学丙医院','合成大学丁医院']
    verified=[]
    class Tools:
        def __init__(self,*a):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False)
        async def search(self,args):
            name=next((name for name in names if name in args['query']),None)
            if name:
                verified.append(name)
                return {'ok':True,'candidates':[{'candidate_id':name,'title':name+'乳腺外科官网','url':'https://example.com/'+name}]}
            return {'ok':True,'candidates':[{'candidate_id':'list','title':'乳腺癌专家排名_最好的专家',
                'snippet':'乳腺癌相关医院与专家介绍','url':'https://example.com/list'}]}
        async def read(self,args):
            if args['candidate_id']=='list':
                return {'ok':True,'sources':[{'id':'list','title':'乳腺癌专家排名_最好的专家','text':'\n'.join(names)}]}
            name=args['candidate_id']
            return {'ok':True,'sources':[{'id':name,'title':name+'乳腺外科','text':'本院开展乳腺癌诊疗服务。'}]}
    monkeypatch.setattr(module,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True),Repo(),'r',
        '有哪些医院比较擅长处理乳腺癌',return_details=True))
    assert set(verified)==set(names) and result['entity_searches']==4
    assert result['directory_entry_count']==4
    assert all(source['id']!='list' for source in result['sources'])


def test_failed_institution_page_tries_an_alternative_under_adaptive_budget(monkeypatch):
    import app.web_tools as module
    names=['合成大学甲医院','合成大学乙医院','合成大学丙医院','合成大学丁医院']
    class Tools:
        def __init__(self,*a):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False)
        async def search(self,args):
            name=next((name for name in names if name in args['query']),None)
            if name:
                return {'ok':True,'candidates':[{'candidate_id':name+suffix,
                    'title':name+'乳腺癌服务','url':'https://example.com/'+name+suffix} for suffix in ['primary','alternative']]}
            return {'ok':True,'candidates':[{'candidate_id':'list','title':'乳腺癌相关医院专家排名','url':'https://example.com/list'}]}
        async def read(self,args):
            identifier=args['candidate_id']
            if identifier=='list':return {'ok':True,'sources':[{'id':'list','title':'乳腺癌相关医院专家排名','text':'\n'.join(names)}]}
            if identifier.endswith('primary'):return {'ok':False,'error':'page_http_error','http_status':403}
            name=identifier.removesuffix('alternative')
            return {'ok':True,'sources':[{'id':name,'title':name+'乳腺外科','text':'本院开展乳腺癌诊疗服务。'}]}
    monkeypatch.setattr(module,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True),Repo(),'r',
        '有哪些医院擅长处理乳腺癌',return_details=True))
    assert result['directory_entry_count']==4 and len(result['sources'])==4
    assert 8<result['direct_reads']<=16
    assert result['retrieval_diagnostics']['discovery_only_sources']==1


def test_public_cache_is_separate_expires_and_rejects_private_or_unverifiable_data(tmp_path):
    import hashlib
    from app.followup.store import Repository
    repo=Repository(tmp_path/'cache.sqlite3')
    url='https://example.com/department';text='合成大学甲医院提供乳腺癌诊疗服务。'
    source={'id':'WEB-'+hashlib.sha256((url+'\n'+text).encode()).hexdigest()[:20],
            'url':url,'title':'合成大学甲医院乳腺外科','text':text,'kind':'public','authority':'unverified'}
    repo.cache_public_sources([source,{**source,'kind':'private'},{**source,'id':'invented'}])
    cached=repo.cached_public_sources()
    assert len(cached)==1 and cached[0]['authority']=='unverified'
    assert cached[0]['retrieval_origin']=='public_web_cache'
    assert repo.items('*','knowledge')==[]
    with repo.db() as db:db.execute('UPDATE public_web_cache SET expires_at=0')
    assert repo.cached_public_sources()==[]


def test_cached_web_evidence_survives_outage_without_changing_region_or_freshness(tmp_path):
    import hashlib
    from app.followup.store import Repository
    repo=Repository(tmp_path/'scope-cache.sqlite3')
    url='https://example.com/department';text='广州合成大学甲医院开展乳腺癌诊疗服务。'
    source={'id':'WEB-'+hashlib.sha256((url+'\n'+text).encode()).hexdigest()[:20],
        'url':url,'title':'合成大学甲医院乳腺外科','text':text,'kind':'public','authority':'unverified',
        'collected_at':'2026-10-18','evidence_institutions':['合成大学甲医院']}
    repo.cache_public_sources([source])
    with repo.db() as db:expiry=db.execute('SELECT expires_at FROM public_web_cache').fetchone()[0]
    settings=Settings(mode='demo',external_enabled=False)
    result=asyncio.run(hospital_sources(settings,repo,'r','广州有哪些医院治疗乳腺癌',return_details=True))
    assert result['cached_source_count']==1 and result['sources'][0]['collected_at']=='2026-10-18'
    assert result['external_status']=='disabled' and result['status']=='partial'
    elsewhere=asyncio.run(hospital_sources(settings,repo,'r2','深圳有哪些医院治疗乳腺癌',return_details=True))
    assert not elsewhere['sources']
    with repo.db() as db:assert db.execute('SELECT expires_at FROM public_web_cache').fetchone()[0]==expiry


def test_failed_answer_review_keeps_cards_not_advertisement_or_navigation_dump():
    from app.followup.rag_pipeline import supported_hospital_answer
    sources=[{'title':'全国十大乳腺外科医院','text':'广告中的NK细胞和高治愈率表述。'}]
    names=['合成大学甲医院','合成大学乙医院','合成大学丙医院','合成大学丁医院']
    sources.extend({'title':name+'乳腺外科','text':'乳腺外科提供乳腺癌相关服务。',
                    'evidence_institutions':[name]} for name in names)
    answer=supported_hospital_answer('有哪些医院比较擅长处理乳腺癌',sources)
    assert all('**'+name+'**' in answer for name in names)
    assert 'NK细胞' not in answer and '高治愈率' not in answer and '> ' not in answer
    assert '原文相关科室' in answer
