"""离线验证公共发现、来源关联、附件和服务回退。不会请求真实 API。"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.web_access import AccessError, Download, PinnedTransport, SafeFetcher, SourcePolicy, clean_url
from app.web_documents import excerpt, parse_html, parse_pdf
from app.web_providers import WebProviders
from app.web_tools import WebTools


@pytest.fixture(autouse=True)
def isolated_runtime_configuration(monkeypatch):
    monkeypatch.setenv("CRAWL4AI_ENABLED", "false")
    monkeypatch.setenv("WEB_PUBLIC_READ_ENABLED", "true")
    monkeypatch.setenv("WEB_DOMESTIC_ONLY", "false")


async def public_dns(host):
    return ["93.184.216.34"]


def setup(handler, monkeypatch, *, seed=("nhc.gov.cn",), tavily="", primary="bocha", resolver=public_dns):
    monkeypatch.setenv("TAVILY_API_KEY", tavily)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", primary)
    monkeypatch.delenv("WEB_VERIFIED_DOMAINS", raising=False)
    settings = SimpleNamespace(bocha_key="fake-bocha", bocha_url="https://search.test/v1/web-search",
                               external_domains=seed)
    transport = httpx.MockTransport(handler)
    ext = SimpleNamespace(settings=settings, transport=transport, resolver=resolver)
    return WebTools(ext, {}, [])


def hits(items):
    return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": items}}})


def html(text="测试官方正文。" * 20, links=""):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                         text="<html><title>测试医院资料</title><article>" + text + links + "</article></html>")


def first(result):
    return result["candidates"][0]["candidate_id"]


def test_unexplained_search_403_recovers_once(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(403, text='Forbidden')
        return hits([{'url':'https://example.com/list','name':'广州医院名单'}])
    web = setup(handler, monkeypatch)
    result = asyncio.run(web.search({'query':'广州 医院 名单'}))
    assert result['ok'] and len(result['candidates']) == 1
    assert len(requests) == 2
    assert result['provider_attempts'][0]['error'] == 'search_access_refused'
    assert result['provider_attempts'][1]['candidate_count'] == 1


@pytest.mark.parametrize('payload,code',[
    ({'message':'余额不足'},'search_quota_error'),
    ({'message':'Invalid API key'},'search_auth_error'),
])
def test_permanent_403_does_not_retry(monkeypatch,payload,code):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(403,json=payload)
    web = setup(handler, monkeypatch)
    result = asyncio.run(web.search({'query':'广州 医院 名单'}))
    assert not result['ok'] and not result['retryable']
    assert len(requests) == 1 and result['provider_attempts'][0]['error'] == code


def test_persistent_unexplained_403_is_bounded(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(403,text='Forbidden')
    web = setup(handler, monkeypatch)
    result = asyncio.run(web.search({'query':'广州 医院 名单'}))
    assert not result['ok'] and not result['retryable']
    assert len(requests) == 2 and len(result['provider_attempts']) == 2


def test_403_tries_backup_before_recovering_primary(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(request.url.host)
        if request.url.host == 'search.test':
            return httpx.Response(403,text='Forbidden')
        return httpx.Response(200,json={'results':[
            {'url':f'https://example.com/{n}','title':'广州医院名单'} for n in range(4)]})
    web=setup(handler,monkeypatch,tavily='test-key')
    result=asyncio.run(web.search({'query':'广州 医院 名单'}))
    assert result['ok'] and len(result['candidates']) == 4
    assert requests == ['search.test','api.tavily.com']


def test_public_discovery_has_no_fixed_include_and_retains_unknown(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return hits([{"url": "https://new-hospital.example/intro", "name": "新医院"},
                     {"url": "https://wsjkw.gd.gov.cn/list", "name": "政府名单"}])

    async def run():
        web = setup(handler, monkeypatch)
        result = await web.search({"query": "某城市 三甲医院"})
        assert "include" not in json.loads(requests[0].content)
        assert len(result["candidates"]) == 2 and len(requests) == 1
        assert result["candidates"][0]["authority"] == "government"
        assert result["candidates"][1]["authority"] == "unverified"
        assert result["evidence_ready"] is False and not web.collected
    asyncio.run(run())


def test_trusted_scope_still_available(monkeypatch):
    def handler(request):
        assert json.loads(request.content)["include"] == "nhc.gov.cn"
        return hits([{"url": "https://nhc.gov.cn/article"}, {"url": "https://unknown.example/"}])

    async def run():
        web = setup(handler, monkeypatch)
        result = await web.search({"query": "医学知识", "scope": "trusted"})
        assert len(result["candidates"]) == 1
    asyncio.run(run())


def test_government_outside_old_registry_can_be_read(monkeypatch):
    def handler(request):
        return hits([{"url": "https://wsjkw.gd.gov.cn/list"}]) if request.method == "POST" else html()

    async def run():
        web = setup(handler, monkeypatch)
        found = await web.search({"query": "名单"})
        result = await web.read({"candidate_id": first(found)})
        assert result["ok"] and result["authority"] == "government"
        assert len(web.collected) == 1
    asyncio.run(run())


def test_unknown_hospital_requires_actual_government_link(monkeypatch):
    monkeypatch.setenv("WEB_PUBLIC_READ_ENABLED", "false")
    visited = []

    def handler(request):
        visited.append(str(request.url))
        if request.method == "POST":
            return hits([{"url": "https://city.gov.cn/hospitals"}])
        if request.url.host == "city.gov.cn":
            return html(links='<a href="https://new-hospital.example/intro">新示例医院</a>')
        return html("新示例医院为测试用三级甲等医院。" * 10)

    async def run():
        web = setup(handler, monkeypatch)
        found = await web.search({"query": "城市医院目录"})
        parent = first(found)
        mapped = await web.map({"candidate_id": parent, "query": "医院"})
        child = first(mapped)
        denied = await web.read({"candidate_id": child})
        assert denied["error"] == "source_unverified"
        verified = await web.verify({"candidate_id": child, "parent_candidate_id": parent})
        assert verified["authority"] == "government_linked"
        read = await web.read({"candidate_id": child})
        assert read["ok"] and len(web.collected) == 1
        assert len(visited) == 3  # 搜索、政府页面、医院正文；拒绝时没有网络请求。
    asyncio.run(run())


def test_unread_or_non_hospital_link_cannot_upgrade_trust(monkeypatch):
    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://city.gov.cn/list"}, {"url": "https://unknown.example/"}])
        return html(links='<a href="https://unknown.example/">广告链接</a>')

    async def run():
        web = setup(handler, monkeypatch)
        found = await web.search({"query": "目录"})
        parent, child = [c["candidate_id"] for c in found["candidates"]]
        result = await web.verify({"candidate_id": child, "parent_candidate_id": parent})
        assert result["error"] == "government_page_not_read"
        await web.read({"candidate_id": parent})
        result = await web.verify({"candidate_id": child, "parent_candidate_id": parent})
        assert result["readable"] and not result["verified"]
        assert result["authority"] == "unverified"
        assert web.policy.classify("https://unknown.example/") == "unverified"
    asyncio.run(run())


def test_source_link_authorization_is_run_local(monkeypatch):
    web = setup(lambda req: hits([]), monkeypatch)
    web.policy.government_linked["new.example"] = {"parent_url": "https://city.gov.cn/"}
    new_web = setup(lambda req: hits([]), monkeypatch)
    assert new_web.policy.classify("https://new.example/") == "unverified"


def test_public_provider_fallback_when_first_is_empty(monkeypatch):
    visited = []

    def handler(request):
        visited.append(request.url.host)
        if request.url.host == "search.test":
            return hits([])
        return httpx.Response(200, json={"results": [{"url": "https://city.gov.cn/list", "title": "名单"}]})

    async def run():
        web = setup(handler, monkeypatch, tavily="fake-tavily")
        result = await web.search({"query": "城市医院"})
        assert result["ok"] and visited == ["search.test", "api.tavily.com"]
        assert len(result["provider_attempts"]) == 2
    asyncio.run(run())


def test_explicit_provider_does_not_silently_switch(monkeypatch):
    visited = []

    def handler(request):
        visited.append(request.url.host)
        return hits([])

    async def run():
        web = setup(handler, monkeypatch, tavily="fake-tavily")
        result = await web.search({"query": "名单", "provider": "bocha"})
        assert result["ok"] and result["outcome"] == "search_empty"
        assert visited == ["search.test"]
    asyncio.run(run())


def test_direct_read_failure_uses_configured_extract(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.host))
        if request.url.host == "search.test":
            return hits([{"url": "https://city.gov.cn/list"}])
        if request.method == "GET":
            return httpx.Response(503)
        assert request.url.path == "/extract"
        return httpx.Response(200, json={"results": [{"url": "https://city.gov.cn/list",
                                                       "raw_content": "正文提取服务读取的官方名单。" * 10}]})

    async def run():
        web = setup(handler, monkeypatch, tavily="fake-tavily")
        # Isolate reader fallback from automatic sparse-result search expansion.
        found = await web.search({"query": "名单", "provider": "bocha"})
        result = await web.read({"candidate_id": first(found)})
        assert result["ok"] and result["reader"] == "tavily_extract"
        assert len(calls) == 3
    asyncio.run(run())


def test_extract_cannot_substitute_other_domain(monkeypatch):
    def handler(request):
        if request.url.host == "search.test":
            return hits([{"url": "https://city.gov.cn/list"}])
        return httpx.Response(200, json={"results": [{"url": "https://other.gov.cn/list",
                                                       "raw_content": "来源被替换。" * 10}]})

    async def run():
        web = setup(handler, monkeypatch, tavily="fake-tavily")
        found = await web.search({"query": "名单"})
        result = await web.read({"candidate_id": first(found), "method": "extract"})
        assert result["error"] == "extract_source_changed" and not web.collected
    asyncio.run(run())


@pytest.mark.parametrize("value", ["https://127.0.0.1/x", "https://[::1]/", "http://localhost/",
                                   "https://x.local/", "https://user:password@city.gov.cn/",
                                   "https://city.gov.cn:444/", "file:///etc/passwd"])
def test_invalid_urls(value):
    with pytest.raises(AccessError):
        clean_url(value)


def test_dns_private_address_prevents_request(monkeypatch):
    async def private_dns(host):
        return ["10.0.0.1"]

    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://city.gov.cn/list"}])
        pytest.fail("私网解析不应产生GET")

    async def run():
        web = setup(handler, monkeypatch, resolver=private_dns)
        found = await web.search({"query": "名单"})
        result = await web.read({"candidate_id": first(found)})
        assert result["error"] == "private_address_blocked"
    asyncio.run(run())


def test_redirect_to_unverified_or_private_is_blocked(monkeypatch):
    monkeypatch.setenv("WEB_PUBLIC_READ_ENABLED", "false")
    gets = []

    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://city.gov.cn/list"}])
        gets.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://unknown.example/"})

    async def run():
        web = setup(handler, monkeypatch)
        found = await web.search({"query": "名单"})
        assert (await web.read({"candidate_id": first(found)}))["error"] == "source_unverified"
        assert len(gets) == 1
    asyncio.run(run())


def test_same_page_different_excerpts_keep_both_source_versions(monkeypatch):
    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://city.gov.cn/list"}])
        return html("开头内容。" * 3000 + "目标甲资料。" * 1800 + "目标乙资料。" * 1800)

    async def run():
        web = setup(handler, monkeypatch)
        found = await web.search({"query": "资料"})
        a = await web.read({"candidate_id": first(found), "query": "目标甲"})
        b = await web.read({"candidate_id": first(found), "query": "目标乙"})
        assert a["ok"] and b["ok"]
        assert a["sources"][0]["id"] != b["sources"][0]["id"]
        assert len(web.collected) == 2 and b["cached"]
        assert "目标甲" in a["sources"][0]["text"]
        assert "目标乙" in b["sources"][0]["text"]
    asyncio.run(run())


def test_excerpt_can_find_information_beyond_first_5000_chars():
    text = "无关内容" * 3000 + "目标医院为三级甲等医院。" * 20
    result = excerpt(text, "目标医院 三级甲等")
    assert "目标医院为三级甲等医院" in result and len(result) <= 7000


def test_html_map_keeps_pdf_links():
    page = parse_html(Download("https://city.gov.cn/list", (
        "<article>" + "政府目录正文。" * 20 + '<a href="/file/list.pdf">医院名单附件</a></article>'
    ).encode(), "text/html"))
    assert page["links"][0]["url"] == "https://city.gov.cn/file/list.pdf"


def test_public_page_too_large_is_rejected(monkeypatch):
    def handler(request):
        if request.method == "POST":
            return hits([{"url": "https://city.gov.cn/list"}])
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 2_000_001)

    async def run():
        web = setup(handler, monkeypatch)
        found = await web.search({"query": "名单"})
        assert (await web.read({"candidate_id": first(found)}))["error"] == "page_too_large"
    asyncio.run(run())


def test_blank_and_unknown_candidate_do_not_request(monkeypatch):
    def handler(request):
        pytest.fail("非法参数不能调用网络")

    async def run():
        web = setup(handler, monkeypatch)
        assert (await web.search({"query": " "}))["error"] == "invalid_arguments"
        assert (await web.read({"candidate_id": "invented"}))["error"] == "unknown_candidate"
        assert (await web.verify({"candidate_id": "invented"}))["error"] == "unknown_candidate"
        assert (await web.map({"candidate_id": "invented"}))["error"] == "unknown_candidate"
    asyncio.run(run())


def test_pinned_transport_preserves_host_tls_and_closes(monkeypatch):
    captured, closed = [], []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"
        async def aclose(self):
            pass

    class FakeTransport:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
        async def handle_async_request(self, request):
            captured.append(request)
            return httpx.Response(200, stream=Stream())
        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", FakeTransport)

    async def run():
        transport = PinnedTransport(public_dns)
        request = httpx.Request("GET", "https://city.gov.cn/list")
        response = await transport.handle_async_request(request)
        assert captured[0].url.host == "93.184.216.34"
        assert captured[0].headers["Host"] == "city.gov.cn"
        assert captured[0].extensions["sni_hostname"] == "city.gov.cn"
        await response.aclose()
        assert closed
    asyncio.run(run())


def test_pdf_text_read_in_child_process():
    import io
    from pypdf import PdfWriter
    # 构造带真实文本操作符的PDF，不依赖reportlab。
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 200 Td (Hospital A is Grade 3A.) Tj ET")
    page[NameObject("/Contents")] = content
    buffer = io.BytesIO()
    writer.write(buffer)
    result = asyncio.run(parse_pdf(Download("https://city.gov.cn/list.pdf", buffer.getvalue(), "application/pdf")))
    assert "Hospital A is Grade 3A" in result["text"] and "PDF第1页" in result["text"]


def test_candidate_payload_is_bounded_without_cutting_urls(monkeypatch):
    from app.web_tools import WebTools
    items = [{"url": "https://city.gov.cn/" + "a" * 1900 + str(i), "title": "标题" * 90,
              "snippet": "摘要" * 250, "candidate_id": str(i)} for i in range(12)]
    bounded = WebTools.bounded(items)
    assert 0 < len(bounded) < 12
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 14000
    assert bounded[-1]["url"] == items[len(bounded) - 1]["url"]


def test_extract_requires_explicit_configuration(monkeypatch):
    def handler(request):
        assert request.method == "POST"
        return hits([{"url": "https://nhc.gov.cn/test"}])
    async def run():
        web = setup(handler, monkeypatch)
        found = await web.search({"query": "资料"})
        result = await web.read({"candidate_id": first(found), "method": "extract"})
        assert result["error"] == "extract_key_missing" and not result["retryable"]
    asyncio.run(run())


def test_scanned_pdf_is_not_reported_as_text():
    import io
    from pypdf import PdfWriter
    writer = PdfWriter(); writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO(); writer.write(buffer)
    with pytest.raises(AccessError, match="pdf_text_empty"):
        asyncio.run(parse_pdf(Download("https://nhc.gov.cn/scanned.pdf", buffer.getvalue(), "application/pdf")))


def test_pdf_worker_timeout_kills_process(monkeypatch):
    import subprocess
    import app.web_documents as documents
    calls = []
    class Worker:
        returncode = None
        def communicate(self, content=None, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("pdf-worker", timeout)
            return b"", b""
        def kill(self):
            calls.append("killed"); self.returncode = -9
    monkeypatch.setattr(documents.subprocess, "Popen", lambda *args, **kwargs: Worker())
    with pytest.raises(AccessError, match="pdf_timeout"):
        asyncio.run(parse_pdf(Download("https://nhc.gov.cn/test.pdf", b"test", "application/pdf")))
    assert calls == ["killed"]


def test_diagnostic_outputs_metadata_not_page_body(monkeypatch):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts/check_web_tools.py"
    spec = importlib.util.spec_from_file_location("web_diagnostic_test", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    def handler(request):
        return hits([{"url": "https://nhc.gov.cn/test"}]) if request.method == "POST" else html("不要输出的正文" * 20)
    async def run():
        web = setup(handler, monkeypatch)
        args = SimpleNamespace(query="公共资料", scope="public", provider="auto", limit=10,
                               read=1, method="auto", map=False)
        result = await module.inspect_web(web, args)
        assert result["reads"][0]["ok"] and result["reads"][0]["sources"][0]["text_chars"] > 0
        serialized = json.dumps(result, ensure_ascii=False)
        assert "不要输出的正文" not in serialized and "fake-bocha" not in serialized
    asyncio.run(run())


def test_event_upgrade_is_dry_run_then_backs_up_and_is_idempotent(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts/upgrade_agent_event_fields.py"
    app = tmp_path / "app"; app.mkdir()
    target = app / "events.py"
    raw = b"\xef\xbb\xbf" + ('class EventLog:\r\n    async def write(self, **details):\r\n'
        '        return {k: v for k, v in details.items() if k in {"status", "tool", "source_count"}}\r\n').encode()
    target.write_bytes(raw)
    dry = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True)
    assert dry.returncode == 0 and target.read_bytes() == raw
    first = subprocess.run([sys.executable, str(script), "--apply"], cwd=tmp_path, capture_output=True)
    assert first.returncode == 0 and b"provider_attempt_count" in target.read_bytes()
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in target.read_bytes()
    backups = list(app.glob("*.bak")); assert len(backups) == 1 and backups[0].read_bytes() == raw
    second = subprocess.run([sys.executable, str(script), "--apply"], cwd=tmp_path, capture_output=True)
    assert second.returncode == 0 and len(list(app.glob("*.bak"))) == 1


def test_event_upgrade_does_not_guess_other_event_implementations(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts/upgrade_agent_event_fields.py"
    app = tmp_path / "app"; app.mkdir()
    target = app / "events.py"; raw = b"class OtherLogger:\n    pass\n"; target.write_bytes(raw)
    result = subprocess.run([sys.executable, str(script), "--apply"], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0 and target.read_bytes() == raw and not list(app.glob("*.bak"))
