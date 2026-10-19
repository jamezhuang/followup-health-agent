"""外部检索的离线回归；不会请求真实 API，也不读取真实密钥。"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.external import ExternalSearch
from app.providers import ProviderError


def make_search(handler, *, domain_filter=True):
    settings = SimpleNamespace(
        bocha_key="test-key",
        bocha_url="https://search.test/v1/web-search",
        external_domains=("nhc.gov.cn", "www.pumch.cn"),
    )
    return ExternalSearch(settings, transport=httpx.MockTransport(handler), domain_filter=domain_filter)


def discovered(urls):
    return httpx.Response(200, json={
        "code": 200,
        "data": {"webPages": {"value": [{"url": u} for u in urls]}},
    })


def article():
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                          content=("<html><title>测试资料</title><article>"
                                   + "离线测试的官方页面正文。" * 15
                                   + "</article></html>").encode())


def test_redirect_and_http_discovery_preserve_real_body():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert json.loads(request.content)["query"] == "抗菌药物"
            return discovered(["http://www.nhc.gov.cn/old"])
        assert request.url.scheme == "https"
        assert "authorization" not in request.headers
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return article()

    result = asyncio.run(make_search(handler).search_detailed("抗菌药物"))
    assert result["diagnostics"]["outcome"] == "ok"
    assert result["diagnostics"]["https_upgraded_count"] == 1
    assert result["sources"][0].url == "https://www.nhc.gov.cn/new"
    assert "离线测试的官方页面正文" in result["sources"][0].text
    assert len(requests) == 3


@pytest.mark.parametrize("target", [
    "https://nhc.gov.cn.evil.test/content",
    "https://evil.test/content",
    "http://www.nhc.gov.cn/content",
    "https://www.nhc.gov.cn:8443/content",
    "https://user:password@www.nhc.gov.cn/content",
])
def test_redirect_cannot_escape_url_policy(target):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return discovered(["https://www.nhc.gov.cn/old"])
        return httpx.Response(302, headers={"location": target})

    result = asyncio.run(make_search(handler).search_detailed("测试"))
    assert result["sources"] == []
    assert result["diagnostics"]["page_results"][0]["reason"] == "redirect_blocked"
    assert len(requests) == 2


@pytest.mark.parametrize("urls,outcome", [
    ([], "search_empty"),
    (["https://other.test/article"], "allowlist_empty"),
])
def test_empty_search_and_domain_filter_are_distinct(urls, outcome):
    def handler(request):
        assert request.method == "POST"
        return discovered(urls)

    result = asyncio.run(make_search(handler).search_detailed("测试"))
    assert result["diagnostics"]["outcome"] == outcome
    assert result["diagnostics"]["attempted_pages"] == 0


@pytest.mark.parametrize("status,content_type,body,reason", [
    (403, "text/html", b"blocked", "page_http_error"),
    (200, "application/pdf", b"pdf", "non_html"),
    (200, "text/html", b"<body>short</body>", "text_too_short"),
    (200, "text/html", b"<body>" + b"a" * 350001, "page_too_large"),
], ids=["http_403", "non_html", "short_body", "oversized_body"])
def test_page_rejections_have_specific_reasons(status, content_type, body, reason):
    def handler(request):
        if request.method == "POST":
            return discovered(["https://www.nhc.gov.cn/article"])
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    result = asyncio.run(make_search(handler).search_detailed("测试"))
    assert result["diagnostics"]["outcome"] == "page_fetch_empty"
    assert result["diagnostics"]["page_results"][0]["reason"] == reason


def test_html_meta_charset_is_used_without_http_charset():
    text = "这是中文健康资料的离线测试正文。" * 15

    def handler(request):
        if request.method == "POST":
            return discovered(["https://www.nhc.gov.cn/article"])
        html = '<html><meta charset="gb2312"><body>' + text + '</body></html>'
        return httpx.Response(200, headers={"content-type": "text/html"},
                              content=html.encode("gb2312"))

    sources = asyncio.run(make_search(handler).search("测试"))
    assert sources[0].text == text


def test_failed_page_does_not_discard_successful_page():
    def handler(request):
        if request.method == "POST":
            return discovered(["https://www.nhc.gov.cn/failed",
                               "https://www.pumch.cn/article"])
        if request.url.path == "/failed":
            raise httpx.ConnectError("PRIVATE-UPSTREAM-BODY", request=request)
        return article()

    result = asyncio.run(make_search(handler).search_detailed("测试"))
    assert len(result["sources"]) == 1
    assert result["diagnostics"]["outcome"] == "ok"
    assert result["diagnostics"]["page_results"][0]["reason"] == "page_network_error"


@pytest.mark.parametrize("response,reason", [
    (httpx.Response(401, text="SECRET-PROVIDER-BODY"), "search_http_error"),
    (httpx.Response(200, json={"code": 403, "msg": "SECRET-PROVIDER-BODY"}),
     "search_business_error"),
    (httpx.Response(200, json={"unexpected": "SECRET-PROVIDER-BODY"}),
     "search_schema_error"),
])
def test_provider_failures_keep_safe_diagnostics(response, reason):
    with pytest.raises(ProviderError) as caught:
        asyncio.run(make_search(lambda _: response).search("测试"))
    assert caught.value.code == "external_unavailable"
    assert caught.value.diagnostics["failure_reason"] == reason
    assert "SECRET-PROVIDER-BODY" not in str(caught.value)
    assert "test-key" not in json.dumps(caught.value.diagnostics)


def test_concurrent_calls_do_not_share_diagnostics():
    def handler(request):
        if request.method == "POST":
            query = json.loads(request.content)["query"]
            return discovered([] if query == "empty" else ["https://www.nhc.gov.cn/a"])
        return article()

    async def run():
        search = make_search(handler)
        return await asyncio.gather(search.search_detailed("empty"),
                                    search.search_detailed("article"))

    empty, full = asyncio.run(run())
    assert empty["diagnostics"]["returned_count"] == 0
    assert full["diagnostics"]["returned_count"] == 1


def test_scope_is_sent_before_search_but_local_policy_still_applies():
    def handler(request):
        body = json.loads(request.content)
        assert set(body["include"].split("|")) == {"nhc.gov.cn", "pumch.cn"}
        assert body["query"] == "原始问题"
        # 即使上游忽略 include，也不下载其他域名。
        return discovered(["https://evil.test/article"])

    result = asyncio.run(make_search(handler).search_detailed("原始问题"))
    assert result["diagnostics"]["outcome"] == "allowlist_empty"
    assert result["diagnostics"]["candidate_host_counts"] == {"evil.test": 1}


def test_medicine_query_does_not_fetch_expert_directory():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(200, json={"data": {"webPages": {"value": [{
            "url": "https://www.pumch.cn/department-doctor-search/451",
            "name": "专家团队 | 北京协和医院",
        }]}}})

    result = asyncio.run(make_search(handler).search_detailed("医生紧急提醒 药不能乱吃"))
    assert result["sources"] == []
    assert result["diagnostics"]["allowed_count"] == 1
    assert result["diagnostics"]["eligible_count"] == 0
    assert result["diagnostics"]["outcome"] == "candidate_filtered_empty"
    assert result["diagnostics"]["filtered_reasons"] == {"directory_page": 1}


def test_explicit_directory_question_keeps_directory_result():
    def handler(request):
        if request.method == "POST":
            return discovered(["https://www.pumch.cn/department-doctor-search/451"])
        return article()

    sources = asyncio.run(make_search(handler).search("查询专家团队及出诊信息"))
    assert len(sources) == 1


def test_directory_discovered_after_redirect_is_not_medical_evidence():
    def handler(request):
        if request.method == "POST":
            return discovered(["https://www.nhc.gov.cn/article"])
        if request.url.path == "/article":
            return httpx.Response(302, headers={"location": "https://www.pumch.cn/department-doctor-search/451"})
        return article()

    result = asyncio.run(make_search(handler).search_detailed("抗菌药物的原理"))
    assert result["sources"] == []
    assert result["diagnostics"]["page_results"][0]["reason"] == "directory_page"


def test_wrong_directory_does_not_displace_article():
    visited = []

    def handler(request):
        if request.method == "POST":
            return discovered([
                "https://www.pumch.cn/department-doctor-search/451",
                "https://www.pumch.cn/article/30204",
            ])
        visited.append(str(request.url))
        return article()

    sources = asyncio.run(make_search(handler).search("医生紧急提醒 药不能乱吃", 1))
    assert sources[0].url.endswith("/article/30204")
    assert visited == ["https://www.pumch.cn/article/30204"]


def test_candidates_are_only_returned_in_explicit_debug_mode():
    search = make_search(lambda _: discovered(["https://other.test/article"]))
    regular = asyncio.run(search.search_detailed("测试"))
    debug = asyncio.run(search.search_detailed("测试", debug=True))
    assert "candidates" not in regular
    assert debug["candidates"][0]["reason"] == "disallowed_domain"
    assert "https://other.test/article" not in json.dumps(debug["diagnostics"])


def test_unscoped_mode_does_not_disable_local_host_validation():
    def handler(request):
        assert "include" not in json.loads(request.content)
        return discovered(["https://evil.test/article"])

    result = asyncio.run(make_search(handler, domain_filter=False).search_detailed("测试"))
    assert result["sources"] == []
    assert result["diagnostics"]["scope_mode"] == "post_filter"


def test_scope_http_400_is_reported_without_automatic_repeat():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, text="SECRET-PROVIDER-BODY")

    with pytest.raises(ProviderError) as caught:
        asyncio.run(make_search(handler).search("测试"))
    assert caught.value.code == "external_scope_rejected"
    assert len(requests) == 1
    assert "SECRET-PROVIDER-BODY" not in str(caught.value)
