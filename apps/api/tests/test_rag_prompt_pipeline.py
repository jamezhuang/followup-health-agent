import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.followup.hospital import hospital_sources
from app.followup.hospital_planner import plan_hospital
from app.followup.hospital_policy import directory_policy
from app.followup.rag_pipeline import (QueryRewrite,EvidenceReview,AnswerReview,rewrite_hospital,
    hospital_queries_safe,verify_answer,review_evidence)
from app.followup.public_rag import public_sources


class Repo:
    def __init__(self,documents=None):self.stages=[];self.documents=documents or []
    def stage(self,*args,**metadata):self.stages.append((args,metadata))
    def items(self,owner,kind):
        assert (owner,kind)==('*','knowledge')
        return self.documents


@pytest.fixture(autouse=True)
def enable(monkeypatch):monkeypatch.setenv('RAG_PROMPT_PIPELINE_ENABLED','true')


def test_rewrite_rejects_widened_scope_missing_filter_and_patient_identifiers():
    plan=plan_hospital('广州有哪些公立三甲医院擅长乳腺癌')
    assert hospital_queries_safe(['广州 三级甲等 公立 乳腺癌 医院 全球顶尖介绍'],plan)==[]
    assert hospital_queries_safe(['广州 三级甲等 公立 乳腺癌 医院 科室介绍'],plan)
    assert not hospital_queries_safe(['深圳 三级甲等 公立 乳腺癌 医院 科室介绍',
        '广州 公立 医院','广州 深圳 三级甲等 公立 乳腺癌 医院','广州 三级甲等 公立 乳腺癌 患者张三医院'],plan)


def test_broken_planner_falls_back_to_existing_queries():
    class Client:
        async def structured(self,*a):raise RuntimeError('untrusted-provider-error')
    plan=plan_hospital('广州有哪些医院');repo=Repo()
    result=asyncio.run(rewrite_hospital(Client(),repo,'run',plan,directory_policy(plan,'广州有哪些医院')))
    assert result==plan.queries(directory_policy(plan,'广州有哪些医院'))
    assert 'untrusted-provider-error' not in str(repo.stages)


def test_multiple_regions_are_separate_queries_and_cannot_be_collapsed():
    goal='比较杭州和东莞有哪些医院擅长处理乳腺癌'
    plan=plan_hospital(goal)
    assert plan.regions==['杭州','东莞'] and plan.care_topic=='乳腺癌'
    policy=directory_policy(plan,goal)
    assert policy['scope']=='multi_region'
    class Client:
        async def structured(self,*a):return QueryRewrite(queries=['杭州 乳腺癌 医院 科室介绍'])
    queries=asyncio.run(rewrite_hospital(Client(),Repo(),'run',plan,policy))
    assert any('杭州' in q for q in queries) and any('东莞' in q for q in queries)


def test_evidence_review_reopens_search_and_retains_successful_sources(monkeypatch):
    import app.web_tools as web_module
    class Tools:
        def __init__(self,*a):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False);self.calls=0
        async def search(self,args):
            self.calls+=1
            return {'ok':True,'candidates':[{'candidate_id':str(self.calls),'title':'示例医院 科室介绍',
                                           'url':'https://example.com/'+str(self.calls)}]}
        async def read(self,args):
            return {'ok':True,'sources':[{'id':args['candidate_id'],'title':'示例医院科室介绍',
                'text':'示例医院设有呼吸内科和儿科。','url':'https://example.com/'+args['candidate_id']}]}
    class Client:
        def __init__(self):self.reviews=0
        async def structured(self,schema,prompt,data):
            if schema is QueryRewrite:return QueryRewrite(queries=['示例医院 科室介绍'])
            assert schema is EvidenceReview
            self.reviews+=1
            return EvidenceReview(sufficient=self.reviews>1,source_ids=[s['id'] for s in data['sources']],
                                  missing=[] if self.reviews>1 else ['service'])
    monkeypatch.setattr(web_module,'WebTools',Tools)
    client=Client();result=asyncio.run(hospital_sources(Settings(mode='live',external_enabled=True),Repo(),
        'run','示例医院有哪些科室',return_details=True,client=client))
    assert result['search_rounds']==2 and len(result['sources'])==2
    assert client.reviews==2 and result['evidence_review']['sufficient']


def test_answer_fact_failure_is_repaired_and_rechecked_once():
    class Client:
        def __init__(self):self.reviews=0;self.repairs=0
        async def structured(self,schema,prompt,data):
            self.reviews+=1
            return AnswerReview(passed=self.reviews>1,issues=[] if self.reviews>1 else ['unsupported_claim'],
                                corrections=[] if self.reviews>1 else ['删除无依据的排名'])
        async def complete(self,messages):self.repairs+=1;return {'content':'示例医院设有呼吸内科。'}
    client=Client();sources=[{'id':'s','title':'科室介绍','text':'示例医院设有呼吸内科。'}]
    text,review=asyncio.run(verify_answer(client,Repo(),'run','有什么科室',sources,'示例医院全国第一。'))
    assert text=='示例医院设有呼吸内科。' and review['status']=='repaired'
    assert client.reviews==2 and client.repairs==1


def test_failed_repair_only_returns_evidence_excerpts():
    class Client:
        async def structured(self,*a):return AnswerReview(passed=False,issues=['unsupported_claim'])
        async def complete(self,*a):return {'content':'仍然编造全国第一。'}
    text,status=asyncio.run(verify_answer(Client(),Repo(),'run','有哪些科室',
        [{'id':'s','title':'科室介绍','text':'示例医院设有呼吸内科。'}],'编造全国第一。'))
    assert '全国第一' not in text and '呼吸内科' in text and status['status']=='supported_excerpts'


def test_public_multitopic_retrieval_uses_both_subqueries_and_published_docs_only():
    docs=[{'id':str(i),'revision':1,'title':title,'status':status,'kind':'public',
           'pieces':[{'location':'正文','text':text}]} for i,(title,status,text) in enumerate([
        ('影像检查','published','CT检查和磁共振检查有不同原理。'),
        ('饮食知识','published','饮食与健康需要均衡营养。'),
        ('草稿','draft','不应进入上下文的草稿内容。')])]
    class Client:
        async def structured(self,schema,prompt,data):
            if schema is QueryRewrite:return QueryRewrite(queries=['CT检查原理','均衡营养饮食'])
            return EvidenceReview(sufficient=True,source_ids=[s['id'] for s in data['sources']])
    result=asyncio.run(public_sources(Settings(mode='live',external_enabled=False),Repo(docs),Client(),
        'run','CT检查原理是什么，也介绍一下均衡营养饮食',[]))
    assert len(result['queries'])==2
    assert {s['document_id'] for s in result['sources']}=={'0','1'}
    assert result['evidence_review']['sufficient']


def test_sufficiency_cannot_claim_evidence_that_was_never_provided():
    class Client:
        async def structured(self,*a):return EvidenceReview(sufficient=True,source_ids=['invented'])
    review=asyncio.run(review_evidence(Client(),Repo(),'run',{'question':'科室'},
        [{'id':'real','text':'示例医院设有呼吸内科。'}]))
    assert review is None


def test_verification_service_failure_is_not_reported_as_passed():
    class Client:
        async def structured(self,*a):raise RuntimeError('upstream-private-error')
    text,status=asyncio.run(verify_answer(Client(),Repo(),'run','科室',
        [{'id':'s','text':'示例医院设有呼吸内科。'}],'原回答'))
    assert text=='原回答' and status['status']=='unavailable'


def test_network_outage_keeps_published_knowledge(monkeypatch):
    import app.web_tools as web_module
    doc={'id':'kb','revision':1,'title':'CT知识','status':'published','kind':'public',
         'pieces':[{'location':'正文','text':'CT检查知识的已有公开正文。'}]}
    class Client:
        async def structured(self,schema,prompt,data):
            if schema is QueryRewrite:return QueryRewrite(queries=['CT检查'])
            return EvidenceReview(sufficient=False,source_ids=[s['id'] for s in data['sources']],missing=['other_requested_information'])
    class Tools:
        def __init__(self,*a):self.providers=SimpleNamespace(available=lambda:['bocha'])
        async def search(self,*a):return {'ok':False,'outcome':'search_failed'}
    monkeypatch.setattr(web_module,'WebTools',Tools)
    result=asyncio.run(public_sources(Settings(mode='live',external_enabled=True),Repo([doc]),Client(),
        'run','CT检查的原理是什么',[]))
    assert result['sources'][0]['document_id']=='kb'
    assert result['external_status']=='service_unavailable'


def test_report_verifier_receives_confirmed_fields_and_keeps_question_scope():
    from app.followup.report_dialogue import answer_report
    class Client:
        async def complete(self,messages):
            assert '不自动附带复诊清单' in messages[0]['content']
            return {'content':'报告记载检查所见为合成描述，报告意见为合成意见。'}
        async def structured(self,schema,prompt,data):
            assert schema is AnswerReview
            fields=data['sources'][0]
            assert fields['id']=='FIELDS-report' and fields['confirmed'] is True
            assert '合成意见' in fields['text']
            return AnswerReview(passed=True)
    document={'id':'report','revision':1,'title':'合成报告','confirmed':True,'kind':'private',
        'pieces':[{'location':'第1页','text':'检查所见：合成描述。报告意见：合成意见。'}],
        'key_fields':{'details':{'findings':'合成描述','impression':'合成意见'}}}
    result=asyncio.run(answer_report(Repo(),Client(),True,'run','解释这份报告的所见和意见',[],[document]))
    assert result['answer_review']['status']=='passed'
    assert not result['questions'] and 'preparation_checklist' not in result


def test_http_live_hospital_route_runs_all_adapted_stages(tmp_path,monkeypatch):
    import httpx
    import app.v2_main as main
    import app.web_tools as web_module
    from app.followup.intent import IntentDecision
    from app.followup.store import Repository
    from app.storage import Store
    called=[]
    class Client:
        def __init__(self,settings,repository):self.settings=settings
        async def structured(self,schema,prompt,data):
            called.append(schema.__name__)
            if schema is IntentDecision:return IntentDecision(intent='daily',consultation_type='hospital',reason_code='hospital_service')
            if schema is QueryRewrite:return QueryRewrite(queries=['示例医院 科室介绍'])
            if schema is EvidenceReview:return EvidenceReview(sufficient=True,source_ids=[s['id'] for s in data['sources']])
            assert schema is AnswerReview
            return AnswerReview(passed=True)
        async def complete(self,*a,**kw):return {'content':'示例医院设有呼吸内科和儿科。'}
    class Tools:
        def __init__(self,*a):
            self.providers=SimpleNamespace(available=lambda:['bocha']);self.browser=SimpleNamespace(enabled=False)
        async def search(self,*a):return {'ok':True,'candidates':[{'candidate_id':'p','title':'示例医院科室介绍','url':'https://example.com/intro'}]}
        async def read(self,*a):return {'ok':True,'sources':[{'id':'source','title':'示例医院科室介绍',
            'text':'示例医院设有呼吸内科和儿科。','url':'https://example.com/intro'}]}
    monkeypatch.setattr(main,'ObservedClient',Client)
    monkeypatch.setattr(web_module,'WebTools',Tools)
    async def scenario():
        repo=Repository(tmp_path/'business.sqlite3');accounts=Store('sqlite:///'+(tmp_path/'auth.sqlite3').as_posix())
        app=main.create_app(Settings(mode='live',external_enabled=True),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
                assert (await api.post('/auth/register',json={'username':'promptroute01','password':'test123456'})).status_code==200
                response=await api.post('/v2/runs',json={'goal':'示例医院有哪些科室'})
                assert response.status_code==200,response.text
                row=response.json()
                for _ in range(300):
                    row=(await api.get('/v2/runs/'+row['id'])).json()
                    if row['status']!='running':break
                    await asyncio.sleep(.01)
                assert row['status']=='completed',row
                assert row['result']['answer_review']['status']=='passed'
                assert row['result']['evidence_review']['sufficient']
                queries = row['result']['query_plan']['queries']
                assert queries[0]=='示例医院 官方网站'
                assert '示例医院 科室介绍' in queries
                assert {'QueryRewrite','EvidenceReview','AnswerReview'}<=set(called)
                assert (await api.get('/v2/subjects')).json()['items']==[]
        accounts.engine.dispose()
    asyncio.run(scenario())
