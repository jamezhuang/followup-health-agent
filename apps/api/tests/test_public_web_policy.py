"""Browser fallback, source boundaries, linked-page reading and Function Calling contracts."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.web_access import AccessError, Download, SourcePolicy, clean_url
from app.web_tools import WebTools
from app.tool_loop import run_tool_loop


class Providers:
    tavily_key = ""
    def available(self): return ["bocha"]


class Fetcher:
    def __init__(self, error=None, content=None):
        self.error, self.content, self.calls = error, content, 0
    async def fetch(self, url):
        self.calls += 1
        if self.error: raise self.error
        return Download(url, self.content, "text/html")


class Browser:
    enabled = True
    def __init__(self): self.calls = 0
    async def fetch(self, url):
        self.calls += 1
        detail = "info_36.aspx" in url
        text = ("模拟科室业务介绍，仅用于测试接口关联与正文引用，不是医学依据。" * 5 if detail else
                "模拟科室导航页面，仅用于测试实际链接发现，不是医学依据。" * 5)
        return {"url":url, "title":"模拟详情" if detail else "模拟导航", "text":text,
                "links":[] if detail else [{"url":"http://pnhqyy.com/ksdh/info_36.aspx?itemid=80&lcid=5", "title":"儿科"}],
                "document_type":"html", "reader":"crawl4ai", "seconds":1,
                "browser_diagnostics":{"domcontentloaded":True}}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("WEB_PUBLIC_READ_ENABLED", "false")
    monkeypatch.setenv("WEB_HTTP_ALLOWED_HOSTS", "pnhqyy.com,www.pnhqyy.com")
    monkeypatch.setenv("WEB_VERIFIED_DOMAINS", "pnhqyy.com")
    settings = SimpleNamespace(external_domains=())
    browser = Browser()
    fetcher = Fetcher(AccessError("page_timeout", retryable=True))
    web = WebTools(SimpleNamespace(settings=settings), {}, [], providers=Providers(), fetcher=fetcher, browser=browser)
    root = web.put({"url":"http://pnhqyy.com/ksdh/index_36.aspx", "title":"模拟导航"})
    return web, browser, fetcher, root


def test_http_opt_in_exact_host(monkeypatch):
    monkeypatch.setenv("WEB_HTTP_ALLOWED_HOSTS", "pnhqyy.com")
    assert clean_url("http://pnhqyy.com/x?a=1") == "http://pnhqyy.com/x?a=1"
    assert clean_url("http://fresh-hospital.example/x").startswith("http://")
    assert clean_url("https://fresh-hospital.example/x").startswith("https://")
    with pytest.raises(AccessError): clean_url("http://127.0.0.1/")


def test_map_fallback_then_detail_read_and_cache(setup):
    web, browser, fetcher, root = setup
    async def run():
        mapped = await web.map({"candidate_id":root["candidate_id"], "query":"儿科"})
        assert mapped["ok"] and mapped["reader"] == "crawl4ai"
        assert mapped["reader_attempts"][0]["error"] == "page_timeout"
        detail = mapped["candidates"][0]
        assert detail["url"].startswith("http://")
        read = await web.read({"candidate_id":detail["candidate_id"]})
        assert read["ok"] and read["sources"][0]["text"]
        assert read["sources"][0]["id"] in web.collected
        again = await web.read({"candidate_id":detail["candidate_id"]})
        assert again["cached"] and browser.calls == 2
    asyncio.run(run())


def public_web(monkeypatch):
    monkeypatch.setenv("WEB_PUBLIC_READ_ENABLED", "true")
    monkeypatch.delenv("WEB_VERIFIED_DOMAINS", raising=False)
    settings = SimpleNamespace(external_domains=())
    html = ('<html><meta charset="utf-8"><title>模拟医院科室</title>'
            '<body><a href="/inpatient">住院部科室</a><main>'
            + '模拟门诊科室文字，仅用于测试，不是真实医院信息。' * 5
            + '</main></body></html>').encode("utf-8")
    web = WebTools(SimpleNamespace(settings=settings), {}, [], providers=Providers(),
                   fetcher=Fetcher(content=html), browser=Browser())
    root = web.put({"url": "https://fresh-hospital.example/departments", "title": "模拟科室"})
    return web, root


def test_public_verify_separates_readable_from_verified(monkeypatch):
    web, root = public_web(monkeypatch)
    result = asyncio.run(web.verify({"candidate_id": root["candidate_id"]}))
    assert result["readable"] and not result["verified"]
    assert result["authority"] == "unverified"
    assert "fresh-hospital.example" not in web.policy.registered


def test_public_reference_is_citable_with_attribution(monkeypatch):
    web, root = public_web(monkeypatch)
    result = asyncio.run(web.read({"candidate_id": root["candidate_id"]}))
    assert result["ok"] and result["evidence_ready"] and result["sources"]
    assert result["requires_attribution"] and result["authority"] == "unverified"
    assert result["sources"][0]["id"] in web.collected


def test_public_directory_citation_is_attributed_not_verified(monkeypatch):
    web, root = public_web(monkeypatch)
    result = asyncio.run(web.read({"candidate_id": root["candidate_id"], "purpose": "hospital_directory"}))
    assert result["ok"] and result["evidence_ready"]
    assert result["authority"] == "unverified" and result["requires_attribution"]
    assert result["citation_scope"] == "attributed_public_statement"
    source = result["sources"][0]
    assert "身份未独立核验" in source["institution"]
    assert source["id"] in web.collected and source["text"]
    assert any("不能冒充" in notice for notice in web.notices)


def test_public_map_discovers_links_without_promoting_authority(monkeypatch):
    web, root = public_web(monkeypatch)
    result = asyncio.run(web.map({"candidate_id": root["candidate_id"], "query": "住院部科室"}))
    assert result["ok"] and result["candidates"]
    assert result["candidates"][0]["url"] == "https://fresh-hospital.example/inpatient"
    assert result["candidates"][0]["authority"] == "unverified"
    assert web.policy.classify(result["candidates"][0]["url"]) == "unverified"


def test_public_read_still_blocks_private_dns(monkeypatch):
    import httpx
    from app.web_access import SafeFetcher
    web, root = public_web(monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="Unexpected private request")
    async def private(host): return ["10.0.0.1"]
    web.fetcher = SafeFetcher(web.policy, transport=httpx.MockTransport(handler), resolver=private)
    result = asyncio.run(web.read({"candidate_id": root["candidate_id"], "purpose": "hospital_directory"}))
    assert result["error"] == "private_address_blocked"
    assert not calls and web.browser.calls == 0


def test_source_refusal_never_starts_browser(setup):
    web, browser, fetcher, _ = setup
    unknown = web.put({"url":"https://unknown.example/", "title":"未知"})
    result = asyncio.run(web.read({"candidate_id":unknown["candidate_id"], "method":"browser"}))
    assert result["error"] == "source_unverified"
    assert browser.calls == fetcher.calls == 0


def test_private_address_failure_never_falls_back(setup):
    web, browser, fetcher, root = setup
    fetcher.error = AccessError("private_address_blocked")
    result = asyncio.run(web.read({"candidate_id":root["candidate_id"]}))
    assert result["error"] == "private_address_blocked" and browser.calls == 0


def test_direct_does_not_use_browser(setup):
    web, browser, _, root = setup
    result = asyncio.run(web.read({"candidate_id":root["candidate_id"], "method":"direct"}))
    assert result["error"] == "page_timeout" and browser.calls == 0


def test_explicit_browser_replaces_insufficient_direct_snapshot(setup):
    web, browser, fetcher, root = setup
    fetcher.error = None
    fetcher.content = ("<html><title>导航</title><body>" + "导航文字" * 40 + "</body></html>").encode()
    first = asyncio.run(web.read({"candidate_id":root["candidate_id"]}))
    second = asyncio.run(web.read({"candidate_id":root["candidate_id"], "method":"browser"}))
    assert first["reader"] == "direct" and second["reader"] == "crawl4ai" and browser.calls == 1
    assert first["sources"][0]["id"] != second["sources"][0]["id"]


def test_function_call_result_keeps_call_id(setup):
    web, browser, _, root = setup
    events = []
    class LLM:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"content":None, "tool_calls":[{"id":"call-browser-1", "type":"function", "function":{
                    "name":"read_webpage", "arguments":json.dumps({"candidate_id":root["candidate_id"], "method":"browser"})}}]}
            payload = messages[-1]
            assert payload["role"] == "tool" and payload["tool_call_id"] == "call-browser-1"
            assert json.loads(payload["content"])["reader"] == "crawl4ai"
            return {"content":'{"summary":"接口测试完成"}'}
    async def emit(stage, **details): events.append((stage, details))
    result = asyncio.run(run_tool_loop(LLM(), [{"role":"user", "content":"测试"}], web.definitions, web.handlers, on_event=emit))
    assert json.loads(result)["summary"] == "接口测试完成"
    assert browser.calls == 1
    assert any(row[0] == "agent_tool" and row[1].get("reader") == "crawl4ai" for row in events)


def test_browser_reader_limits_calls_and_checks_addresses(monkeypatch):
    monkeypatch.setenv("WEB_BROWSER_MAX_READS", "5")
    from app import browser_reader as module
    monkeypatch.setenv("CRAWL4AI_ENABLED", "true")
    monkeypatch.setenv("WEB_VERIFIED_DOMAINS", "pnhqyy.com")
    async def checked(url): return ["8.8.8.8"]
    monkeypatch.setattr(module, "checked_addresses", checked)
    async def runner(url): return {"url":url, "text":"模拟正文" * 30}
    reader = module.Crawl4AIReader(SourcePolicy(SimpleNamespace(external_domains=())), runner=runner)
    async def run():
        for _ in range(5): await reader.fetch("https://pnhqyy.com/")
        with pytest.raises(AccessError, match="browser_budget_exceeded"):
            await reader.fetch("https://pnhqyy.com/")
    asyncio.run(run())


@pytest.mark.parametrize("query,text", [
    ("城市三级甲等医院", "测试医院简介记载：本院为三级甲等综合医院。"),
    ("中医院科室", "测试中医院科室导航列出：眼科、耳鼻喉科、骨科。"),
    ("眼科医生专长", "测试医生介绍：甲医生，主任医师，擅长眼科常见疾病诊疗。"),
])
def test_new_question_types_get_citable_public_body(monkeypatch, query, text):
    # 合成内容用于验证引用链，不是医院名录或医生事实。
    web, root = public_web(monkeypatch)
    web.fetcher.content = ("<html><meta charset='utf-8'><title>测试公开信息</title>"
                           "<body><main>" + text * 8 + "</main></body></html>").encode("utf-8")
    result = asyncio.run(web.read({"candidate_id": root["candidate_id"], "query": query}))
    assert result["ok"] and result["evidence_ready"] and result["requires_attribution"]
    source = result["sources"][0]
    assert text in source["text"] and source["id"] in web.collected
    assert web.policy.classify(source["url"]) == "unverified"


def test_search_returns_first_five_public_candidates(monkeypatch):
    web, _ = public_web(monkeypatch)
    requested = []
    class SearchProviders(Providers):
        async def search(self, query, limit, provider, scope):
            requested.append((limit, scope))
            return {"ok": True, "candidates": [{"url": f"https://site{i}.example/", "title": f"候选{i}"}
                    for i in range(limit)]}
    web.providers = SearchProviders()
    result = asyncio.run(web.search({"query": "测试城市 医院"}))
    assert requested == [(5, "public")]
    assert len(result["candidates"]) == 5
    assert not result["evidence_ready"]


def test_partial_tool_failure_keeps_other_body_and_call_ids(monkeypatch):
    web, root = public_web(monkeypatch)
    class LLM:
        count = 0
        async def complete(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                calls = []
                for identity, candidate in [("failed", "missing"), ("success", root["candidate_id"])]:
                    calls.append({"id": identity, "type": "function", "function": {
                        "name": "read_webpage", "arguments": json.dumps({"candidate_id": candidate})}})
                return {"content": None, "tool_calls": calls}
            rows = [m for m in messages if m["role"] == "tool"]
            assert [m["tool_call_id"] for m in rows] == ["failed", "success"]
            assert not json.loads(rows[0]["content"])["ok"]
            assert json.loads(rows[1]["content"])["sources"]
            return {"content": '{"summary":"返回已读取部分"}'}
    result = asyncio.run(run_tool_loop(LLM(), [{"role": "user", "content": "接口测试"}],
                                     web.definitions, web.handlers))
    assert json.loads(result)["summary"] == "返回已读取部分" and web.collected


def test_agent_patch_removes_conflicting_rules_and_is_idempotent():
    import ast
    from pathlib import Path
    from scripts.apply_public_web_policy import patch_agent
    # 安装后的真实文件用于检查集成；本地验证可通过变量指定旧版本。
    import os
    path = Path(os.getenv("POLICY_TEST_AGENT_PATH", str(Path(__file__).resolve().parents[1] / "app/agent.py")))
    source = path.read_text(encoding="utf-8-sig")
    changed = patch_agent(source)
    assert "TOOL_POLICY_PROMPT" in changed
    assert "【多轮追问与引用规则】" not in changed
    assert "当用户询问具体医院的科室、业务范围、就诊流程" not in changed
    assert patch_agent(changed) == changed
    compile(changed, "agent.py", "exec")
    assert ast.parse(changed)


def test_env_patch_keeps_keys_and_is_idempotent():
    from scripts.apply_public_web_policy import patch_env
    original = "DEEPSEEK_API_KEY=synthetic-test-key\nWEB_PUBLIC_READ_ENABLED=false\n"
    changed = patch_env(original)
    assert "DEEPSEEK_API_KEY=synthetic-test-key" in changed
    assert "WEB_PUBLIC_READ_ENABLED=true" in changed
    assert patch_env(changed) == changed


def test_cancelled_worker_is_reaped_and_slot_released(monkeypatch, tmp_path):
    import os
    import sys
    from app import browser_reader as module
    app = tmp_path / "app"
    app.mkdir()
    pid_file = tmp_path / "worker.pid"
    (app / "crawl4ai_worker.py").write_text(
        'import os, pathlib, time\npathlib.Path("worker.pid").write_text(str(os.getpid()))\ntime.sleep(30)\n',
        encoding="utf-8")
    monkeypatch.setattr(module, "API_ROOT", tmp_path)
    monkeypatch.setenv("CRAWL4AI_PYTHON", sys.executable)
    reader = module.Crawl4AIReader(SourcePolicy(SimpleNamespace(external_domains=())))
    async def run():
        task = asyncio.create_task(reader._worker("https://example.com/"))
        for _ in range(100):
            if pid_file.exists(): break
            await asyncio.sleep(.01)
        assert pid_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert module._BROWSER_SLOT.acquire(blocking=False)
        module._BROWSER_SLOT.release()
        if os.name != "nt":
            with pytest.raises(ProcessLookupError): os.kill(int(pid_file.read_text()), 0)
    asyncio.run(run())
