"""Provider contracts and fallback paths; no paid network calls."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.web_access import SourcePolicy
from app.web_providers import WebProviders


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv('BAIDU_SEARCH_API_KEY','fake-baidu')
    monkeypatch.setenv('TAVILY_API_KEY','fake-tavily')
    monkeypatch.setenv('WEB_SEARCH_PROVIDER','baidu')
    monkeypatch.setenv('WEB_DOMESTIC_ONLY','true')


def provider(handler, bocha_key='fake-bocha'):
    settings=SimpleNamespace(bocha_key=bocha_key,bocha_url='https://bocha.test/',external_domains=())
    return WebProviders(settings,SourcePolicy(settings),transport=httpx.MockTransport(handler))


def test_baidu_contract_does_not_treat_snippet_as_evidence():
    def handler(request):
        assert str(request.url)=='https://qianfan.baidubce.com/v2/ai_search/web_search'
        assert request.headers['X-Appbuilder-Authorization']=='Bearer fake-baidu'
        body=json.loads(request.content)
        assert body['messages']==[{'role':'user','content':'示例医院 科室'}]
        assert body['search_source']=='baidu_search_v2'
        assert body['resource_type_filter']==[{'type':'web','top_k':20}]
        return httpx.Response(200,json={'references':[
            {'title':'示例医院','url':'https://example.com/hospital','content':'仅搜索摘要'},
            {'title':'invalid','url':'http://127.0.0.1/private'}]})
    p=provider(handler,bocha_key='')
    assert p.available()==['baidu']
    result=asyncio.run(p.search('示例医院 科室',5))
    assert result['ok'] and len(result['candidates'])==1
    candidate=result['candidates'][0]
    assert candidate['provider']=='baidu' and candidate['authority']=='unverified'
    assert candidate['snippet']=='仅搜索摘要' and 'text' not in candidate


@pytest.mark.parametrize('failure',['empty','unauthorized','business_error','schema_error'])
def test_baidu_empty_or_failure_falls_back_to_bocha(failure):
    visited=[]
    def handler(request):
        visited.append(request.url.host)
        if request.url.host=='qianfan.baidubce.com':
            if failure=='unauthorized': return httpx.Response(401)
            if failure=='business_error': return httpx.Response(200,json={'code':'AccessDenied','message':'not logged'})
            return httpx.Response(200,json={'references':[] if failure=='empty' else 'invalid'})
        return httpx.Response(200,json={'code':200,'data':{'webPages':{'value':[
            {'name':'示例医院','url':'https://example.com/hospital'}]}}})
    result=asyncio.run(provider(handler).search('示例医院',5))
    assert visited==['qianfan.baidubce.com','bocha.test']
    assert result['ok'] and result['candidates'][0]['provider']=='bocha'
    assert len(result['provider_attempts'])==2
    assert 'fake-baidu' not in str(result)


def test_missing_baidu_key_uses_only_configured_service(monkeypatch):
    monkeypatch.setenv('BAIDU_SEARCH_API_KEY','')
    visited=[]
    def handler(request):
        visited.append(request.url.host)
        return httpx.Response(200,json={'data':{'webPages':{'value':[]}}})
    p=provider(handler)
    assert p.available()==['bocha']
    result=asyncio.run(p.search('示例医院',5))
    assert result['outcome']=='search_empty' and visited==['bocha.test']


def test_search_attempt_telemetry_keeps_only_safe_fields(tmp_path):
    from app.followup.store import Repository
    repo=Repository(tmp_path/'telemetry.sqlite3')
    repo.stage('run','Hospital','search',provider_attempts=[
        {'provider':'baidu','error':'search_http_error','http_status':401,
         'query':'PRIVATE_PATIENT','key':'SECRET_KEY','response':'PRIVATE_BODY'},
        {'provider':'bocha','raw_count':50,'candidate_count':12,'url':'PRIVATE_URL'},
        {'provider':'SECRET_PROVIDER','raw_count':1}],browser_reads=2,failed_pages=1)
    stage=repo.trace('run')['stages'][0]
    assert stage['provider_attempts']==[
        {'provider':'baidu','error':'search_http_error','http_status':401},
        {'provider':'bocha','raw_count':50,'candidate_count':12}]
    assert stage['browser_reads']==2 and stage['failed_pages']==1
    assert 'PRIVATE' not in str(stage) and 'SECRET' not in str(stage)
