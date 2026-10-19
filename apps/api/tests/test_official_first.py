import asyncio
from types import SimpleNamespace
from app.config import Settings
from app.source_registry import official_sites_for_names
from app.followup.hospital_planner import plan_hospital
from app.followup.hospital import hospital_sources, _usable, _missing_names, _candidate_score

class Repo:
    def stage(self,*args,**kwargs):pass

def test_composite_alias_does_not_become_parent_plus_unrelated_hospital():
    names=plan_hospital('南方医科大学珠江医院三水医院地址在哪').names
    assert names==['南方医科大学珠江医院三水医院']
    assert set(official_sites_for_names(names))=={'sshospital.cn'}
    assert set(official_sites_for_names(['南方医科大学珠江医院']))=={'zjyy.com.cn'}
    assert official_sites_for_names(['合成珠江医院三水医院'])=={}
    child={'title':'南方医科大学珠江医院三水医院','text':'医院地址：合成路1号。','url':'https://www.sshospital.cn/'}
    parent=plan_hospital('南方医科大学珠江医院地址')
    assert not _usable(parent,child) and _candidate_score(parent,child)<0
    assert _missing_names(plan_hospital('南方医科大学珠江医院三水医院地址'),[{'title':'佛山市三水区人民医院','text':'地址：合成路1号。'}])==[]

def test_address_reads_official_home_and_real_navigation_before_search(monkeypatch):
    from app import web_tools
    reads=[]
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:[])
            self.browser=SimpleNamespace(enabled=False)
        def put(self,item):return {**item,'candidate_id':'home'}
        async def read(self,args):
            reads.append(args)
            text='南方医科大学珠江医院三水医院 医院概况' if args['candidate_id']=='home' else '南方医科大学珠江医院三水医院，地址：合成路1号。'
            return {'ok':True,'authority':'registered','sources':[{'id':args['candidate_id'],'title':'南方医科大学珠江医院三水医院','text':text,'url':'https://www.sshospital.cn/'+('' if args['candidate_id']=='home' else 'intro')}]}
        async def map(self,args):
            return {'ok':True,'candidates':[{'candidate_id':'offsite','title':'医院概况','url':'https://thirdparty.example/intro'},
                {'candidate_id':'intro','title':'医院概况','url':'https://www.sshospital.cn/intro'},
                {'candidate_id':'duplicate','title':'医院简介','url':'https://www.sshospital.cn/intro'}]}
        async def search(self,args):raise AssertionError('Official address already supported')
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True,hospital_page_limit=2),Repo(),'r',
        '患者张三在佛山市三水区人民医院，地址在哪里',return_details=True))
    assert [r['candidate_id'] for r in reads]==['home','intro']
    assert all('张三' not in r['query'] for r in reads)
    assert result['direct_reads']==2 and result['search_rounds']==0
    assert 'address' in result['covered_topics']

def test_failed_official_read_leaves_room_for_search_fallback(monkeypatch):
    from app import web_tools
    reads=[]
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False)
        def put(self,item):return {**item,'candidate_id':'home'}
        async def read(self,args):
            reads.append(args['candidate_id'])
            if args['candidate_id']=='home':return {'ok':False,'error':'page_network_error'}
            return {'ok':True,'authority':'unverified','sources':[{'id':'fallback','title':'佛山市三水区人民医院地址','text':'佛山市三水区人民医院地址：合成路1号。','url':'https://thirdparty.example/address'}]}
        async def search(self,args):return {'ok':True,'candidates':[{'candidate_id':'fallback','title':'佛山市三水区人民医院地址','url':'https://thirdparty.example/address'}]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True,hospital_page_limit=2),Repo(),'r','佛山市三水区人民医院地址',return_details=True))
    assert reads==['home','fallback'] and result['direct_reads']==2
    assert result['official_entry_checks'][0]['read_ok'] is False
    assert result['sources'][0]['authority']=='unverified'

def test_unregistered_hospital_follows_actual_website_link_without_certification(monkeypatch):
    from app import web_tools
    reads=[]
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False)
        async def search(self,args):return {'ok':True,'candidates':[{'candidate_id':'profile','title':'远方大学附属医院地址','url':'https://profile.example/hospital'}]}
        async def read(self,args):
            reads.append(args['candidate_id'])
            url='https://profile.example/hospital' if args['candidate_id']=='profile' else 'https://new-hospital.example/'
            return {'ok':True,'authority':'unverified','sources':[{'id':args['candidate_id'],'title':'远方大学附属医院','text':'远方大学附属医院地址：合成路1号。','url':url}]}
        async def map(self,args):return {'ok':True,'candidates':[{'candidate_id':'linked','title':'医院官网','url':'https://new-hospital.example/'},{'candidate_id':'ad','title':'广告','url':'https://ad.example/'}]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True,hospital_page_limit=3),Repo(),'r','远方大学附属医院地址',return_details=True))
    assert reads==['profile','linked']
    assert result['discovered_website_links'][0]['parent_url']=='https://profile.example/hospital'
    assert all(source['authority']=='unverified' for source in result['sources'])

def test_failed_publisher_document_recovers_only_its_discovered_origin(monkeypatch):
    from app import web_tools
    reads=[];origins=[]
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False)
        def put(self,item):origins.append(item['url']);return {**item,'candidate_id':'origin'}
        async def search(self,args):return {'ok':True,'candidates':[{'candidate_id':'old','url':'https://new-hospital.example/old/page','title':'联系我们-远方大学附属医院','publisher_discovery_score':28}]}
        async def read(self,args):
            reads.append(args['candidate_id'])
            if args['candidate_id']=='old':return {'ok':False,'error':'page_http_error'}
            return {'ok':True,'authority':'unverified','sources':[{'id':'origin','title':'远方大学附属医院','text':'远方大学附属医院地址：合成路1号。','url':'https://new-hospital.example/'}]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True,hospital_page_limit=3),Repo(),'r','远方大学附属医院地址',return_details=True))
    assert origins==['https://new-hospital.example/'] and reads==['old','origin']
    assert result['direct_reads']==2 and result['sources'][0]['authority']=='unverified'

def test_unregistered_hospital_discovers_url_without_authority_upgrade(monkeypatch):
    from app import web_tools
    assert official_sites_for_names(['远方大学附属医院'])=={}
    queries=[];reads=[]
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False)
        async def search(self,args):
            queries.append(args['query'])
            return {'ok':True,'candidates':[
                {'candidate_id':'aggregator','title':'远方大学附属医院地址电话交通','url':'https://platform.example/hospital/address'},
                {'candidate_id':'discovered','title':'远方大学附属医院','url':'https://new-hospital.example/'}]}
        async def read(self,args):
            reads.append(args['candidate_id'])
            return {'ok':True,'authority':'unverified','sources':[{'id':args['candidate_id'],'title':'远方大学附属医院','text':'远方大学附属医院地址：合成路1号。','url':'https://new-hospital.example/'}]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(mode='demo',external_enabled=True),Repo(),'r','远方大学附属医院地址',return_details=True))
    assert '官方网站' in queries[0] and reads[0]=='discovered'
    assert result['sources'] and all(s['authority']=='unverified' for s in result['sources'])
    assert official_sites_for_names(['远方大学附属医院'])=={}
