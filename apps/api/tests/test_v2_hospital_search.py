import asyncio
from types import SimpleNamespace
from app.followup.hospital import hospital_sources, hospital_query
from app.followup.chat import chat
from app.config import Settings

class Repo:
    def __init__(self): self.events=[]
    def stage(self,*args,**kwargs): self.events.append(kwargs)

def test_public_discovery_reads_body_and_preserves_attribution(monkeypatch):
    from app import web_tools
    class Tools:
        def __init__(self,*args):
            self.read_ids=[]
            self.providers=SimpleNamespace(available=lambda:['bocha'])
            self.browser=SimpleNamespace(enabled=False)
        async def search(self,args):
            assert args['scope']=='public'
            assert '张三' not in args['query']
            return {'ok':True,'candidates':[
                {'candidate_id':'right','title':'普宁康美医院','snippet':'科室介绍'},
                {'candidate_id':'wrong','title':'普宁市人民医院','snippet':'科室介绍'},
                {'candidate_id':'failed','title':'普宁康美医院','snippet':'不可直接引用的摘要'}]}
        async def read(self,args):
            assert args['candidate_id']!='wrong'
            if args['candidate_id']=='failed': return {'ok':False}
            return {'ok':True,'authority':'unverified','sources':[{'id':'web-1',
                'title':'医院介绍','text':'普宁康美医院设内科','url':'https://example.com/hospital'}]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    repo=Repo()
    result=asyncio.run(hospital_sources(Settings(external_enabled=True,bocha_key='test'),
        repo,'run','普宁康美医院有哪些科室，患者张三血压180'))
    assert len(result)==1
    assert result[0]['authority']=='unverified'
    assert '未独立核验' in result[0]['location']
    assert '摘要' not in result[0]['text']

def test_hospital_failure_acknowledges_supplied_name():
    result=asyncio.run(chat(Repo(),None,True,'run','普宁康美医院有哪些科室',[],False,intent='hospital'))
    assert '普宁康美医院' in result['content']
    assert '请提供医院的完整名称' not in result['content']
    assert result['sources']==[]
    assert hospital_query('帮我查普宁康美医院有哪些科室')[0]=='普宁康美医院'
    assert hospital_query('我爸张三血压180，哪里挂号') is None
    assert hospital_query('我爸张三在普宁康美医院复查血压')[0]=='普宁康美医院'
    assert hospital_query('患者张三普宁康美医院挂号') is None


def test_browser_retries_are_bounded_and_preserve_direct_evidence(monkeypatch):
    from app import web_tools
    calls=[]
    class Tools:
        def __init__(self,*args):
            self.providers=SimpleNamespace(available=lambda:['baidu'])
            self.browser=SimpleNamespace(enabled=True)
        async def search(self,args):
            return {'ok':True,'candidates':[{'candidate_id':str(i),'title':'示例医院'} for i in range(5)]}
        async def read(self,args):
            calls.append((args['candidate_id'],args['method']))
            if args['method']=='direct' and args['candidate_id']!='0':
                return {'ok':False,'error':'text_too_short'}
            return {'ok':True,'authority':'unverified','sources':[{
                'id':args['candidate_id'],'title':'示例医院','text':'示例医院设内科',
                'url':'https://example.com/'+args['candidate_id']}]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(hospital_sources(Settings(external_enabled=True,bocha_key=''),Repo(),'run','示例医院有哪些科室'))
    assert len(result)==3 and result[0]['id']=='0'
    assert len([call for call in calls if call[1]=='browser'])==2
