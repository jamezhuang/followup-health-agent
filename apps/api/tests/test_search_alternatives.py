import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.tencent_search import parse_pages, TencentSearchFailure
from app.web_access import SourcePolicy
from app.web_providers import WebProviders
import app.web_providers as providers_module
from app.followup.chat import grounded_hospital_names


@pytest.fixture
def configure(monkeypatch):
    for key in ('BAIDU_SEARCH_API_KEY', 'TAVILY_API_KEY', 'SEARXNG_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('WEB_DOMESTIC_ONLY', 'false')
    monkeypatch.setenv('WEB_SEARCH_PROVIDER', 'bocha')
    settings = SimpleNamespace(bocha_key='test', bocha_url='https://search.test/', external_domains=(),
                               tencent_search_enabled=False, tencent_secret_id='test-id', tencent_secret_key='test-key')
    return settings


def test_tencent_pages_are_only_candidate_snippets():
    rows = parse_pages({'Pages': [json.dumps({'url':'https://wjw.sz.gov.cn/list',
                         'title':'医院名录','passage':'<b>医院</b> &amp; 信息'}), '{bad', None]})
    assert rows == [{'url':'https://wjw.sz.gov.cn/list', 'title':'医院名录', 'snippet':' 医院  & 信息'}]
    assert parse_pages({'Pages': []}) == []
    with pytest.raises(TencentSearchFailure):
        parse_pages({'Pages': ['{bad']})


def test_tencent_disabled_does_not_reuse_ocr_credentials(configure):
    provider = WebProviders(configure, SourcePolicy(configure))
    assert provider.available() == ['bocha']


def test_tencent_service_failure_falls_back_without_becoming_empty(configure, monkeypatch):
    configure.tencent_search_enabled = True
    monkeypatch.setenv('WEB_SEARCH_PROVIDER', 'tencent')
    async def unavailable(settings, query):
        raise TencentSearchFailure('search_service_unavailable')
    monkeypatch.setattr(providers_module, 'search_tencent', unavailable)
    def handler(request):
        return httpx.Response(200, json={'data':{'webPages':{'value':[
            {'url':'https://wjw.sz.gov.cn/list','name':'深圳名录'}]}}})
    provider = WebProviders(configure, SourcePolicy(configure), httpx.MockTransport(handler))
    result = asyncio.run(provider.search('深圳 医院', 8))
    assert result['ok'] and result['candidates'][0]['provider'] == 'bocha'
    assert result['provider_attempts'][0]['error'] == 'search_service_unavailable'
    failed = asyncio.run(provider.search('深圳 医院', 8, provider='tencent'))
    assert not failed['ok'] and failed['outcome'] == 'search_failed'


@pytest.mark.parametrize('case', ['hits', 'empty', 'engine_failure', 'disabled_json', 'bad_schema'])
def test_searxng_contract(configure, monkeypatch, case):
    monkeypatch.setenv('SEARXNG_URL', 'http://127.0.0.1:8088')
    monkeypatch.setenv('WEB_SEARCH_PROVIDER', 'searxng')
    def handler(request):
        assert request.url.path == '/search'
        assert request.url.params['format'] == 'json' and request.url.params['q'] == '深圳 医院'
        if case == 'disabled_json':
            return httpx.Response(403)
        data = {'results': []}
        if case == 'hits':
            data['results'] = [{'url':'http://127.0.0.1/private','title':'blocked'},
                               {'url':'https://wjw.sz.gov.cn/list','title':'名录','content':'公开信息'}]
        elif case == 'engine_failure':
            data['unresponsive_engines'] = [['engine', 'timeout']]
        elif case == 'bad_schema':
            data = []
        return httpx.Response(200, json=data)
    provider = WebProviders(configure, SourcePolicy(configure), httpx.MockTransport(handler))
    result = asyncio.run(provider.search('深圳 医院', 8, provider='searxng'))
    if case == 'hits':
        assert result['ok'] and len(result['candidates']) == 1
        assert result['candidates'][0]['snippet'] == '公开信息'
    elif case == 'empty':
        assert result['ok'] and result['outcome'] == 'search_empty'
    else:
        assert not result['ok'] and result['outcome'] == 'search_failed'


def test_domestic_mode_requires_searxng_upstream_declaration(configure, monkeypatch):
    monkeypatch.setenv('SEARXNG_URL', 'http://127.0.0.1:8088')
    monkeypatch.setenv('WEB_DOMESTIC_ONLY', 'true')
    monkeypatch.setenv('SEARXNG_DOMESTIC_ONLY', 'false')
    assert 'searxng' not in WebProviders(configure, SourcePolicy(configure)).available()
    monkeypatch.setenv('SEARXNG_DOMESTIC_ONLY', 'true')
    assert 'searxng' in WebProviders(configure, SourcePolicy(configure)).available()


def test_directory_answer_coverage_counts_location_suffix():
    name = '中山大学附属第七医院（深圳）'
    assert grounded_hospital_names(f'**{name}**', [{'text':name}]) == [name]


def test_sparse_primary_is_supplemented_without_losing_previous_hits(configure, monkeypatch):
    monkeypatch.setenv('SEARXNG_URL', 'http://127.0.0.1:8088')
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'data':{'webPages':{'value':[
                {'url':'https://wjw.sz.gov.cn/first','name':'第一条'}]}}})
        return httpx.Response(200, json={'results':[
            {'url':'https://wjw.sz.gov.cn/first','title':'重复'},
            *[{'url':f'https://wjw.sz.gov.cn/{i}','title':'补充'} for i in range(3)]]})
    provider = WebProviders(configure, SourcePolicy(configure), httpx.MockTransport(handler))
    result = asyncio.run(provider.search('深圳 医院', 8))
    assert len(result['candidates']) == 4 and len(result['provider_attempts']) == 2
    assert result['candidates'][0]['provider'] == 'bocha'


def test_funnel_trace_persists_counts_and_drops_unapproved_payloads(tmp_path):
    from app.followup.store import Repository
    repo=Repository(tmp_path/'trace.sqlite3')
    repo.stage('run','医院查询工具','保存检索诊断',stop_reason='page_budget_exhausted',
        retrieval_diagnostics={'returned_candidates':12,'rejected_sources':2,
            'raw_patient_text':'private','page_errors':{'page_http_error':3,'secret-key':4}})
    # Open a fresh repository to verify this is actually persisted.
    stage=Repository(tmp_path/'trace.sqlite3').trace('run')['stages'][0]
    assert stage['stop_reason']=='page_budget_exhausted'
    assert stage['retrieval_diagnostics']=={'returned_candidates':12,'rejected_sources':2,
                                         'page_errors':{'page_http_error':3}}
