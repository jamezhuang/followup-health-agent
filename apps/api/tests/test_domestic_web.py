"""只配置国内搜索时的离线合同，不消耗任何API额度。"""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.web_tools import WebTools
from app.agent import Agent
import app.agent as agent_module
from app.schemas import Verification


@pytest.fixture(autouse=True)
def domestic_configuration(monkeypatch):
    monkeypatch.setenv("WEB_DOMESTIC_ONLY", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "bocha")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.delenv("WEB_VERIFIED_DOMAINS", raising=False)
    monkeypatch.setenv("CRAWL4AI_ENABLED", "false")
    monkeypatch.setenv("WEB_PUBLIC_READ_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_V2_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_V3_ENABLED", "true")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_ENABLED", "false")


async def dns(host):
    return ["93.184.216.34"]


def make_external(handler):
    settings = SimpleNamespace(bocha_key="fake-bocha", bocha_url="https://search.test/", external_domains=())
    return SimpleNamespace(settings=settings, transport=httpx.MockTransport(handler), resolver=dns)


def hits(items):
    return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": items}}})


def html(text):
    return httpx.Response(200, text="<article>" + text + "</article>",
                          headers={"content-type": "text/html; charset=utf-8"})


def test_only_configured_services_are_advertised():
    web = WebTools(make_external(lambda req: hits([])), {}, [])
    search, _, read, _ = web.definitions
    assert search["function"]["parameters"]["properties"]["provider"]["enum"] == ["auto", "bocha"]
    assert read["function"]["parameters"]["properties"]["method"]["enum"] == ["auto", "direct"]
    assert "Tavily" not in read["function"]["description"]


def test_domestic_search_can_advertise_local_browser_without_foreign_key(monkeypatch):
    monkeypatch.setenv("CRAWL4AI_ENABLED", "true")
    web = WebTools(make_external(lambda req: hits([])), {}, [])
    search, _, read, mapping = web.definitions
    assert search["function"]["parameters"]["properties"]["provider"]["enum"] == ["auto", "bocha"]
    assert read["function"]["parameters"]["properties"]["method"]["enum"] == ["auto", "direct", "browser"]
    assert mapping["function"]["parameters"]["properties"]["method"]["enum"] == ["auto", "direct", "browser"]
    assert "extract" not in read["function"]["parameters"]["properties"]["method"]["enum"]


def test_domestic_switch_blocks_leftover_foreign_keys(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-leftover")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    web = WebTools(make_external(lambda req: pytest.fail("不应请求网络")), {}, [])
    assert web.providers.available() == ["bocha"] and not web.providers.tavily_key
    assert web.providers.primary == "bocha"
    result = asyncio.run(web.search({"query": "医院", "provider": "tavily"}))
    assert result["error"] == "provider_not_configured"
    assert result["available_providers"] == ["bocha"]


def test_empty_bocha_search_does_not_attempt_missing_service():
    visited = []
    def handler(request):
        visited.append(request.url.host)
        return hits([])
    web = WebTools(make_external(handler), {}, [])
    result = asyncio.run(web.search({"query": "医院"}))
    assert visited == ["search.test"] and result["outcome"] == "search_empty"
    assert len(result["provider_attempts"]) == 1


def test_registered_hospital_is_read_without_government_page(monkeypatch):
    monkeypatch.setenv("WEB_VERIFIED_DOMAINS", "example-hospital.cn")
    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://www.example-hospital.cn/intro", "name": "示例医院简介"}])
        return html("示例医院是测试用三级甲等医院。" * 10)
    async def run():
        web = WebTools(make_external(handler), {}, [])
        found = await web.search({"query": "示例医院 医院简介"})
        result = await web.read({"candidate_id": found["candidates"][0]["candidate_id"]})
        assert result["ok"] and result["authority"] == "registered"
        assert "三级甲等" in result["sources"][0]["text"]
    asyncio.run(run())


def test_unconfigured_extract_is_blocked_without_network():
    web = WebTools(make_external(lambda req: pytest.fail("不应请求网络")), {}, [])
    candidate = web.put({"url": "https://city.gov.cn/list"})
    result = asyncio.run(web.read({"candidate_id": candidate["candidate_id"], "method": "extract"}))
    assert result["error"] == "extract_key_missing"


def test_model_recovers_from_dead_directory_to_hospital_intro(monkeypatch):
    monkeypatch.setenv("WEB_VERIFIED_DOMAINS", "example-hospital.cn")
    visited = []
    def handler(request):
        visited.append((request.method, request.url.host))
        if request.method == "POST":
            query = json.loads(request.content)["query"]
            url = "https://city.gov.cn/old-list" if query == "某城市 三甲医院" else "https://www.example-hospital.cn/intro"
            return hits([{"url": url, "name": "医院资料"}])
        return httpx.Response(404) if request.url.host == "city.gov.cn" else html("示例医院是三级甲等综合医院。" * 10)

    def call(call_id, name, arguments):
        return {"content": None, "tool_calls": [{"id": call_id, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
    def results(messages):
        return [json.loads(row["content"]) for row in messages if row["role"] == "tool"]
    def read(call_id):
        return lambda messages: call(call_id, "read_webpage", {
            "candidate_id": results(messages)[-1]["candidates"][0]["candidate_id"]})
    def rewrite(messages):
        assert results(messages)[-1]["http_status"] == 404
        return call("search-again", "search_web", {"query": "示例医院 医院简介 三级甲等", "provider": "bocha"})
    def final(messages):
        source = results(messages)[-1]["sources"][0]
        return {"content": json.dumps({"summary": "先列出已核实的一家，不是完整名单。", "answer_basis": "retrieved",
             "claims": [{"text": "示例医院简介说明其为三级甲等。", "quote": "示例医院是三级甲等综合医院。",
                         "evidence_id": source["id"]}]}, ensure_ascii=False)}
    class LLM:
        def __init__(self):
            self.sequence = iter([call("search", "search_web", {"query": "某城市 三甲医院"}),
                                  read("read-old"), rewrite, read("read-intro"), final])
            self.requests = []
            self.verification_requests = []
        async def structured(self, schema, prompt, data):
            assert schema is Verification
            self.verification_requests.append(copy.deepcopy(data))
            return Verification(supported=True, reason="国内工具协议测试的确定性复核替身")
        async def complete(self, messages, **kwargs):
            self.requests.append(copy.deepcopy(messages))
            step = next(self.sequence)
            return step(messages) if callable(step) else step
    class Events:
        async def write(self, *args, **kwargs):
            pass
    async def local(*args):
        return []
    monkeypatch.setattr(agent_module, "local_service_query", lambda *args: None)
    monkeypatch.setattr(agent_module, "search_local_with_events", local)
    agent = Agent.__new__(Agent); agent.settings = SimpleNamespace(mode="live", external_enabled=True)
    agent.client = LLM(); agent.external = make_external(handler); agent.events = Events(); agent.search = object()
    state = {"message": "某城市有哪些三甲医院", "facts": {}, "history": [], "memory_context": {},
             "confirmed_reports": [], "run_id": "domestic-test"}
    result = asyncio.run(agent.retrieve(state))
    assert result["answer"].answer_basis == "retrieved" and len(result["sources"]) == 1
    assert visited == [("POST", "search.test"), ("GET", "city.gov.cn"),
                       ("POST", "search.test"), ("GET", "www.example-hospital.cn")]
