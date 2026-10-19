"""完全离线：MockTransport + 固定模型计划，不读取 .env 或消耗 API 额度。"""

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent import Agent
import app.agent as agent_module
from app.external import ExternalSearch
from app.providers import ProviderError
from app.tool_loop import ToolLoopError, run_tool_loop
from app.web_tools import WebTools
from app.schemas import Verification


@pytest.fixture(autouse=True)
def isolated_web_environment(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "bocha")
    monkeypatch.delenv("WEB_VERIFIED_DOMAINS", raising=False)
    monkeypatch.setenv("CRAWL4AI_ENABLED", "false")
    monkeypatch.setenv("WEB_DOMESTIC_ONLY", "true")
    monkeypatch.setenv("WEB_PUBLIC_READ_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_V2_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_V3_ENABLED", "true")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_ENABLED", "false")


def call(call_id, name, args):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }]}


def answer(**extra):
    return {"content": json.dumps({"summary": "资料不足。", "answer_basis": "insufficient",
                                   **extra}, ensure_ascii=False)}


class FakeLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.verification_requests = []

    async def structured(self, schema, prompt, data):
        # 本文件验证工具协议；内容复核用独立确定性替身，不消费工具计划。
        # 真正的拒绝/误拒绝/修正路径由test_agent_reliability_v3覆盖。
        assert schema is Verification
        self.verification_requests.append(copy.deepcopy(data))
        return Verification(supported=True, reason="工具协议测试的确定性复核替身")

    async def complete(self, messages, **extra):
        self.requests.append((copy.deepcopy(messages), extra))
        item = next(self.responses)
        return item(messages) if callable(item) else item


class Events:
    def __init__(self):
        self.rows = []

    async def write(self, run_id, stage, **details):
        self.rows.append({"run_id": run_id, "stage": stage, **details})


def external(handler):
    settings = SimpleNamespace(bocha_key="fake-test-key",
                               bocha_url="https://search.test/v1/web-search",
                               external_domains=("nhc.gov.cn",))
    result = ExternalSearch(settings, httpx.MockTransport(handler), domain_filter=True)
    async def public_dns(host):
        return ["93.184.216.34"]
    result.resolver = public_dns
    return result


def hits(items):
    return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": items}}})


def html(text="离线官方测试资料。" * 20):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                         text="<title>测试文章</title><article>" + text + "</article>")


def tool_results(messages):
    return [json.loads(row["content"]) for row in messages if row["role"] == "tool"]


def read_first(call_id):
    def response(messages):
        candidates = tool_results(messages)[-1]["candidates"]
        return call(call_id, "read_webpage", {"candidate_id": candidates[0]["candidate_id"]})
    return response


def cite_last(messages):
    source = tool_results(messages)[-1]["sources"][0]
    return answer(summary="检索得到测试资料。", answer_basis="retrieved", claims=[{
        "text": "正文包含测试资料。", "evidence_id": source["id"], "quote": source["text"][:20],
    }])


def make_agent(llm, search, monkeypatch, local_sources=None):
    agent = Agent.__new__(Agent)
    agent.settings = SimpleNamespace(mode="live", external_enabled=True)
    agent.client = llm
    agent.external = search
    agent.search = object()
    agent.events = Events()

    async def local(*args):
        return local_sources or []

    monkeypatch.setattr(agent_module, "search_local_with_events", local)
    monkeypatch.setattr(agent_module, "local_service_query", lambda *args: None)
    return agent


def state():
    return {"message": "请查官方资料解释这个问题，并提供来源。", "history": [], "facts": {},
            "memory_context": {}, "confirmed_reports": [], "run_id": "test-run"}


def test_discovery_does_not_download_and_ninth_candidate_is_visible():
    requests = []

    def handler(request):
        requests.append(request)
        return hits([{"url": f"https://nhc.gov.cn/article/{i}", "name": f"文章{i}"}
                     for i in range(9)])

    async def run():
        web = WebTools(external(handler), {}, [])
        result = await web.search({"query": "公共知识", "limit": 9})
        assert len(result["candidates"]) == 9
        assert result["evidence_ready"] is False
        assert "sources" not in result
        assert requests[0].method == "POST" and len(requests) == 1
        assert "include" not in json.loads(requests[0].content)
    asyncio.run(run())


def test_default_discovery_returns_five_without_downloading():
    requests = []
    def handler(request):
        requests.append(request)
        return hits([{"url": f"https://nhc.gov.cn/article/{i}", "name": f"文章{i}"}
                     for i in range(9)])
    async def run():
        web = WebTools(external(handler), {}, [])
        result = await web.search({"query": "公共知识"})
        assert len(result["candidates"]) == 5
        assert result["evidence_ready"] is False and "sources" not in result
        assert len(requests) == 1 and requests[0].method == "POST"
    asyncio.run(run())


def test_model_can_choose_ninth_candidate(monkeypatch):
    downloaded = []

    def handler(request):
        if request.method == "POST":
            return hits([{"url": f"https://nhc.gov.cn/article/{i}", "name": f"文章{i}"}
                         for i in range(9)])
        downloaded.append(str(request.url))
        return html()

    def select_ninth(messages):
        item = tool_results(messages)[-1]["candidates"][8]
        return call("read9", "read_webpage", {"candidate_id": item["candidate_id"]})

    llm = FakeLLM([call("local", "search_knowledge", {"query": "公共知识", "top_k": 3}),
                   call("discover", "search_web", {"query": "公共知识", "limit": 9}),
                   select_ninth, cite_last])
    agent = make_agent(llm, external(handler), monkeypatch)
    result = asyncio.run(agent.retrieve(state()))
    assert downloaded == ["https://nhc.gov.cn/article/8"]
    assert result["answer"].answer_basis == "retrieved"
    assert len(result["sources"]) == 1
    assert [row["tool_call_id"] for row in llm.requests[-1][0] if row["role"] == "tool"] == [
        "local", "discover", "read9"]
    definitions = llm.requests[0][1]["tools"]
    assert {t["function"]["name"] for t in definitions} == {
        "search_knowledge", "search_web", "read_webpage", "verify_source", "map_website"}


def test_page_412_is_returned_to_model_then_query_is_rewritten(monkeypatch):
    queries = []

    def handler(request):
        if request.method == "POST":
            queries.append(json.loads(request.content)["query"])
            url = "https://nhc.gov.cn/blocked" if len(queries) == 1 else "https://nhc.gov.cn/good"
            return hits([{"url": url, "name": "官方文章"}])
        return httpx.Response(412) if request.url.path == "/blocked" else html()

    def rewrite(messages):
        assert tool_results(messages)[-1]["error"] == "page_http_error"
        return call("search2", "search_web", {"query": "公共知识 另一种问法"})

    llm = FakeLLM([call("local", "search_knowledge", {"query": "公共知识"}),
                   call("search1", "search_web", {"query": "公共知识"}),
                   read_first("read1"), rewrite, read_first("read2"), cite_last])
    agent = make_agent(llm, external(handler), monkeypatch)
    result = asyncio.run(agent.retrieve(state()))
    assert result["sources"][0].url.endswith("/good")
    assert queries == ["公共知识", "公共知识 另一种问法"]
    assert any(r.get("error_code") == "page_http_error" for r in agent.events.rows)
    assert any(r.get("tool_call_id") == "read2" for r in agent.events.rows)


def test_unread_candidate_citation_is_repaired(monkeypatch):
    def handler(request):
        assert request.method == "POST"
        return hits([{"url": "https://nhc.gov.cn/test", "name": "候选标题"}])

    llm = FakeLLM([call("search", "search_web", {"query": "公共知识"}),
                   answer(summary="试图引用摘要。", answer_basis="retrieved", claims=[{
                       "text": "错误引用", "evidence_id": "CAND-invented", "quote": "候选摘要"}]),
                   answer(summary="尚未取得正文，不能引用摘要。")])
    agent = make_agent(llm, external(handler), monkeypatch)
    result = asyncio.run(agent.retrieve(state()))
    assert result["answer"].answer_basis == "insufficient"
    assert result["sources"] == []
    assert any(r.get("error_code") == "invalid_citation" for r in agent.events.rows)


def test_read_only_current_run_candidate_and_deny_other_urls():
    def handler(request):
        pytest.fail("未知候选不应产生网络请求")

    async def run():
        web = WebTools(external(handler), {}, [])
        assert (await web.read({"candidate_id": "made-up"}))["error"] == "unknown_candidate"
        assert (await web.read({"candidate_id": "x", "url": "http://127.0.0.1"}))["error"] == "invalid_arguments"
        assert (await web.search({"query": " "}))["error"] == "invalid_arguments"
        assert (await web.search({"query": "公共知识", "limit": 100}))["error"] == "invalid_arguments"
    asyncio.run(run())


def test_search_deny_redirect_private_and_disallowed_domains():
    visited = []

    def handler(request):
        visited.append(str(request.url))
        if request.method == "POST":
            return hits([{"url": "https://nhc.gov.cn/test"},
                         {"url": "https://evil.test/"}, {"url": "http://127.0.0.1"}])
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    async def run():
        web = WebTools(external(handler), {}, [])
        found = await web.search({"query": "公共知识"})
        assert len(found["candidates"]) == 2
        result = await web.read({"candidate_id": found["candidates"][0]["candidate_id"]})
        assert result["error"] == "literal_ip_blocked"
        assert len(visited) == 2
    asyncio.run(run())


@pytest.mark.parametrize("status,retryable", [(401, False), (400, False), (429, True), (503, True)])
def test_search_error_retry_policy(status, retryable):
    async def run():
        web = WebTools(external(lambda req: httpx.Response(status, text="hidden-provider-secret")), {}, [])
        result = await web.search({"query": "公共知识"})
        assert result["retryable"] is retryable
        assert "hidden-provider-secret" not in json.dumps(result)
    asyncio.run(run())


def test_duplicate_success_is_cached_and_tool_ids_stay_associated():
    invoked = []

    async def handler(args):
        invoked.append(args)
        return {"ok": True, "sources": []}

    llm = FakeLLM([call("a", "lookup", {"query": "same"}),
                   call("b", "lookup", {"query": "same"}), answer()])
    asyncio.run(run_tool_loop(llm, [], [], {"lookup": handler}))
    results = [r for r in llm.requests[-1][0] if r["role"] == "tool"]
    assert len(invoked) == 1
    assert [r["tool_call_id"] for r in results] == ["a", "b"]
    assert json.loads(results[1]["content"])["cached"] is True


def test_transient_failure_only_reexecutes_twice():
    invoked = []

    async def handler(args):
        invoked.append(args)
        return {"ok": False, "error": "search_timeout", "retryable": True}

    llm = FakeLLM([call("a", "lookup", {}), call("b", "lookup", {}),
                   call("c", "lookup", {}), answer()])
    asyncio.run(run_tool_loop(llm, [], [], {"lookup": handler}))
    assert len(invoked) == 2
    assert tool_results(llm.requests[-1][0])[-1]["error"] == "repeated_tool_call"


def test_bad_arguments_unknown_tool_and_handler_error_do_not_abort_loop():
    async def crash(args):
        raise RuntimeError("private body or key")

    malformed = call("a", "lookup", {})
    malformed["tool_calls"][0]["function"]["arguments"] = "{bad"
    llm = FakeLLM([malformed, call("b", "not_registered", {}), call("c", "lookup", {}), answer()])
    asyncio.run(run_tool_loop(llm, [], [], {"lookup": crash}))
    results = tool_results(llm.requests[-1][0])
    assert [r["error"] for r in results] == ["invalid_arguments", "unknown_tool", "tool_internal"]
    assert "private body or key" not in json.dumps(results)


def test_tool_timeout_returned_for_model_recovery():
    async def slow(args):
        await asyncio.sleep(1)

    llm = FakeLLM([call("a", "slow", {}), answer()])
    asyncio.run(run_tool_loop(llm, [], [], {"slow": slow}, tool_timeout=0.01))
    assert tool_results(llm.requests[-1][0])[0]["error"] == "tool_timeout"


def test_call_budget_keeps_result_for_every_call_then_disables_tools():
    first = call("a", "lookup", {})
    first["tool_calls"].append(call("b", "lookup", {})["tool_calls"][0])

    async def lookup(args):
        return {"ok": True}

    llm = FakeLLM([first, answer()])
    asyncio.run(run_tool_loop(llm, [], [], {"lookup": lookup}, max_calls=2))
    assert llm.requests[-1][1]["tool_choice"] == "none"
    assert len(tool_results(llm.requests[-1][0])) == 2


def test_model_timeout_is_bounded():
    class SlowLLM:
        async def complete(self, *args, **kwargs):
            await asyncio.sleep(1)

    with pytest.raises(ToolLoopError):
        asyncio.run(run_tool_loop(SlowLLM(), [], [], {}, total_timeout=0.03, final_reserve=0.01))


def test_legacy_external_name_is_compatible(monkeypatch):
    from app.schemas import Source
    source = Source(id="WEB-old", title="旧测试资料", institution="测试医院",
                    url="https://nhc.gov.cn/test", published_at=None,
                    collected_at="2026-10-08", text="旧工具返回的正文。", allowed_departments=[],
                    kind="external_excerpt")
    ext = external(lambda req: pytest.fail("旧接口的替身应接管请求"))

    async def old_search(query, top_k=3):
        return [source]

    ext.search = old_search
    llm = FakeLLM([call("old", "search_official_web", {"query": "旧工具"}), cite_last])
    agent = make_agent(llm, ext, monkeypatch)
    result = asyncio.run(agent.retrieve(state()))
    assert result["sources"][0].id == "WEB-old"


def test_local_only_does_not_claim_external_read(monkeypatch):
    llm = FakeLLM([call("local", "search_knowledge", {"query": "公共知识"}), answer()])
    agent = make_agent(llm, external(lambda req: pytest.fail("不能请求外网")), monkeypatch)
    result = asyncio.run(agent.retrieve(state()))
    assert result["retrieval_notice"] == ""
    assert all(r.get("tool") != "search_web" for r in agent.events.rows)


def test_multiple_pages_can_be_read_then_combined(monkeypatch):
    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://nhc.gov.cn/first", "name": "资料一"},
                         {"url": "https://nhc.gov.cn/second", "name": "资料二"}])
        return html(("资料一的正文。" if request.url.path == "/first" else "资料二的正文。") * 20)

    def read_two(messages):
        candidates = tool_results(messages)[-1]["candidates"]
        result = call("read_a", "read_webpage", {"candidate_id": candidates[0]["candidate_id"]})
        result["tool_calls"].extend(call("read_b", "read_webpage", {
            "candidate_id": candidates[1]["candidate_id"]})["tool_calls"])
        return result

    def combine(messages):
        results = tool_results(messages)[-2:]
        return answer(summary="综合两篇已读正文。", answer_basis="retrieved", claims=[{
            "text": "此来源包含测试资料。", "evidence_id": r["sources"][0]["id"],
            "quote": r["sources"][0]["text"][:20],
        } for r in results])

    llm = FakeLLM([call("search", "search_web", {"query": "公共知识"}), read_two, combine])
    agent = make_agent(llm, external(handler), monkeypatch)
    result = asyncio.run(agent.retrieve(state()))
    assert len(result["sources"]) == 2
    assert len(result["answer"].claims) == 2
    assert [r["tool_call_id"] for r in llm.requests[-1][0] if r["role"] == "tool"] == [
        "search", "read_a", "read_b"]


def test_invalid_tool_payload_is_reported_without_crashing():
    async def invalid(args):
        return {"ok": True, "value": object()}

    llm = FakeLLM([call("a", "lookup", {}), answer()])
    asyncio.run(run_tool_loop(llm, [], [], {"lookup": invalid}))
    assert tool_results(llm.requests[-1][0])[0]["error"] == "invalid_tool_result"


def test_duplicate_call_id_is_rejected():
    async def handler(args):
        return {"ok": True}

    batch = call("same-id", "lookup", {})
    batch["tool_calls"].extend(call("same-id", "lookup", {})["tool_calls"])
    llm = FakeLLM([batch])
    with pytest.raises(ToolLoopError):
        asyncio.run(run_tool_loop(llm, [], [], {"lookup": handler}))


@pytest.mark.parametrize("status,retryable", [(412, False), (503, True)])
def test_page_status_has_retry_metadata(status, retryable):
    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://nhc.gov.cn/test"}])
        return httpx.Response(status)

    async def run():
        web = WebTools(external(handler), {}, [])
        found = await web.search({"query": "公共知识"})
        result = await web.read({"candidate_id": found["candidates"][0]["candidate_id"]})
        assert result["retryable"] is retryable
        assert result["http_status"] == status
    asyncio.run(run())


def test_agent_can_follow_government_directory_verify_then_read(monkeypatch):
    # 明确验证可选的严格来源模式；线上公共读取默认仍开启。
    monkeypatch.setenv("WEB_PUBLIC_READ_ENABLED", "false")
    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://city.gov.cn/list", "name": "医院目录"}])
        if request.url.host == "city.gov.cn":
            return html("本页列出当地医院的官方入口。" * 10 +
                        '<a href="https://new-hospital.example/intro">新示例医院</a>')
        return html("新示例医院是测试用三级甲等医院。" * 10)

    def map_directory(messages):
        return call("map", "map_website", {"candidate_id": tool_results(messages)[-1]["candidates"][0]["candidate_id"]})

    def premature_read(messages):
        return call("first_read", "read_webpage", {"candidate_id": tool_results(messages)[-1]["candidates"][0]["candidate_id"]})

    def verify_after_denial(messages):
        results = tool_results(messages)
        assert results[-1]["error"] == "source_unverified"
        child = results[-2]["candidates"][0]
        return call("verify", "verify_source", {"candidate_id": child["candidate_id"],
                                                "parent_candidate_id": child["parent_candidate_id"]})

    def read_after_verify(messages):
        return call("second_read", "read_webpage", {"candidate_id": tool_results(messages)[-1]["candidate_id"]})

    llm = FakeLLM([call("local", "search_knowledge", {"query": "当地医院"}),
                   call("search", "search_web", {"query": "当地医院 政府目录"}),
                   map_directory, premature_read, verify_after_denial, read_after_verify, cite_last])
    agent = make_agent(llm, external(handler), monkeypatch)
    current = state(); current["message"] = "请查询某城市有哪些三甲医院"
    result = asyncio.run(agent.retrieve(current))
    assert result["answer"].answer_basis == "retrieved"
    assert result["sources"][0].url.startswith("https://new-hospital.example/")
    assert any(r.get("authority") == "government_linked" for r in agent.events.rows)


def test_hospital_list_cannot_skip_all_external_search(monkeypatch):
    searches = []
    def handler(request):
        searches.append(request)
        return hits([])
    llm = FakeLLM([answer(), call("search", "search_web", {"query": "某城市 三甲医院"}), answer()])
    agent = make_agent(llm, external(handler), monkeypatch)
    current = state(); current["message"] = "请查询某城市有哪些三甲医院"
    result = asyncio.run(agent.retrieve(current))
    assert result["answer"].answer_basis == "insufficient" and len(searches) == 1
    assert any(r.get("error_code") == "external_search_not_attempted" for r in agent.events.rows)


def test_hospital_list_general_answer_requires_evidence(monkeypatch):
    def handler(request):
        return hits([{"url": "https://city.gov.cn/list"}]) if request.method == "POST" else html()
    llm = FakeLLM([answer(summary="凭常识列出某医院。", answer_basis="general"),
                   call("search", "search_web", {"query": "某城市 三甲医院"}), read_first("read"), cite_last])
    agent = make_agent(llm, external(handler), monkeypatch)
    current = state(); current["message"] = "请查询某城市有哪些三甲医院"
    result = asyncio.run(agent.retrieve(current))
    assert result["answer"].answer_basis == "retrieved"
    assert any(r.get("error_code") == "hospital_list_needs_sources" for r in agent.events.rows)


def test_tool_call_ids_are_unique_across_rounds():
    async def lookup(args):
        return {"ok": True}
    llm = FakeLLM([call("same", "lookup", {}), call("same", "lookup", {"query": "different"})])
    with pytest.raises(ToolLoopError):
        asyncio.run(run_tool_loop(llm, [], [], {"lookup": lookup}))


def test_cross_excerpt_quote_is_rejected(monkeypatch):
    from app.schemas import Source
    source = Source(id="TEST-SPLIT", title="测试", institution="测试",
                    url="https://nhc.gov.cn/test", published_at=None, collected_at="2026-10-08",
                    text="片段甲。[原文片段边界]片段乙。", allowed_departments=[], kind="external_excerpt")
    def handler(request):
        return hits([{"url": "https://nhc.gov.cn/test"}])
    llm = FakeLLM([call("local", "search_knowledge", {"query": "公共知识"}),
                   call("search", "search_web", {"query": "公共知识"}),
                   answer(answer_basis="retrieved", claims=[{"text": "试图拼接引用", "evidence_id": source.id,
                                                               "quote": source.text}]), answer()])
    agent = make_agent(llm, external(handler), monkeypatch, [source])
    result = asyncio.run(agent.retrieve(state()))
    assert result["answer"].answer_basis == "insufficient"
    assert any(r.get("error_code") == "invalid_citation" for r in agent.events.rows)
