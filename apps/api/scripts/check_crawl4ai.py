"""Standalone local browser diagnostic; does not import or change the Agent."""
from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit


def public_url(value: str) -> str:
    """Basic CLI input check, not a production SSRF/source-verification policy."""
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must start with http:// or https://")
    if parts.username or parts.password:
        raise ValueError("URLs containing credentials are not supported")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Use --self-test for the built-in local fixture")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private IP URLs are not supported")
    if parts.port not in {None, 80, 443}:
        raise ValueError("Only standard HTTP/HTTPS ports are supported")
    return urldefrag(value.strip())[0]  # Preserve HTTP; do not force HTTPS.


def failure_reason(error: str) -> str:
    value = error.lower()
    if "anti-bot" in value or "captcha" in value:
        return "possible_block_page"
    if "executable doesn't exist" in value or "executable not found" in value:
        return "browser_missing"
    if "timeout" in value or "timed out" in value:
        return "timeout"
    if "err_name_not_resolved" in value or "getaddrinfo" in value:
        return "dns_error"
    if "err_cert" in value or "certificate" in value:
        return "tls_error"
    if any(term in value for term in ("err_connection", "err_tunnel", "err_proxy", "err_empty_response", "err_network")):
        return "connection_error"
    return "browser_error"


def collect_links(result, base_url: str) -> list[dict]:
    links = []
    seen = set()
    for group in ("internal", "external"):
        for item in (result.links or {}).get(group, []):
            href = item.get("href", "")
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            try:
                target = public_url(urljoin(base_url, href))
            except ValueError:
                continue
            if target in seen:
                continue
            seen.add(target)
            label = " ".join(str(item.get("text") or item.get("title") or "").split())
            internal = urlsplit(target).hostname == urlsplit(base_url).hostname
            links.append({"url": target, "text": label[:300], "same_host": internal})
            if len(links) >= 300:
                return links
    return links


def save_result(result, url: str, folder: Path, keywords: list[str]) -> dict:
    from bs4 import BeautifulSoup

    final_url = result.redirected_url or result.url or url
    markdown = getattr(result.markdown, "raw_markdown", "") or ""
    html = result.html or ""
    cleaned = result.cleaned_html or ""
    soup = BeautifulSoup(cleaned, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    status = result.redirected_status_code or result.status_code
    error = result.error_message or ""
    if status is not None and not 200 <= status < 300:
        reason = "http_error"
    elif not result.success:
        reason = failure_reason(error)
    elif status is None:
        reason = "status_unknown"
    elif len(text) < 80 or not markdown.strip():
        reason = "content_empty"
    elif any(term in text.lower() for term in
             ("verify you are human", "checking your browser", "access denied", "人机验证")):
        reason = "possible_block_page"
    else:
        reason = "ok"

    links = collect_links(result, final_url)
    terms = ("科室", "业务", "简介", "门诊", "department")
    candidates = [link for link in links
                  if any(term in (link["text"] + " " + link["url"]).lower() for term in terms)]
    candidates.sort(key=lambda item: (not item["same_host"], -len(item["text"])))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page.md").write_text(markdown[:100_000], encoding="utf-8")
    (folder / "page.html").write_text(html[:2_000_000], encoding="utf-8")
    (folder / "text.txt").write_text(text[:80_000], encoding="utf-8")
    if result.screenshot:
        (folder / "screenshot.png").write_bytes(base64.b64decode(result.screenshot))
    return {
        "url": url, "final_url": final_url,
        "title": (result.metadata or {}).get("title") or "",
        "crawl_success": bool(result.success), "http_status": status,
        "readable": reason == "ok", "reason": reason,
        "error": error[:1500], "text_chars": len(text),
        "markdown_chars": len(markdown), "preview": text[:800],
        "keyword_matches": {word: word in text for word in keywords},
        "department_link_candidates": candidates[:20], "links": links,
        "source_verified": False,
        "note": "Readable content and candidate links do not establish hospital identity or relevance.",
        "saved_content_truncated": len(html) > 2_000_000 or len(markdown) > 100_000 or len(text) > 80_000,
    }


async def inspect_page(args, url: str, folder: Path) -> dict:
    started = time.monotonic()
    folder.mkdir(parents=True, exist_ok=True)
    # Do not leave a previous successful page beside a new failed result.
    for name in ("page.md", "page.html", "text.txt", "screenshot.png", "result.json"):
        (folder / name).unlink(missing_ok=True)
    report = {"url": url, "readable": False, "reason": "not_started"}
    navigation = {"main_response": None, "domcontentloaded": False,
                  "load": False, "failed_requests": []}

    async def observe_navigation(page, **_):
        def response_received(response):
            request = response.request
            if request.is_navigation_request() and request.frame == page.main_frame:
                navigation["main_response"] = {"url": response.url, "status": response.status}

        def request_failed(request):
            if len(navigation["failed_requests"]) < 10:
                navigation["failed_requests"].append({
                    "url": urlsplit(request.url)._replace(query="", fragment="").geturl(),
                    "resource_type": request.resource_type, "error": request.failure,
                })

        page.on("response", response_received)
        page.on("requestfailed", request_failed)
        page.on("domcontentloaded", lambda: navigation.update(domcontentloaded=True))
        page.on("load", lambda: navigation.update(load=True))
        return page

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

        browser = BrowserConfig(
            browser_type="chromium", channel=args.browser, chrome_channel=args.browser,
            headless=not args.headed, ignore_https_errors=False,
            java_script_enabled=True, accept_downloads=False, verbose=False,
        )
        config = CrawlerRunConfig(
            cache_mode=CacheMode.DISABLED,
            wait_until=args.wait_until, page_timeout=args.timeout * 1000,
            delay_before_return_html=args.wait,
            word_count_threshold=1, excluded_tags=["script", "style", "noscript"],
            remove_forms=True, scan_full_page=args.scan,
            max_scroll_steps=3, scroll_delay=0.3, screenshot=args.screenshot,
            verbose=False,
        )

        async def run():
            # A new browser/context prevents diagnostic attempts sharing cookies or cache.
            async with AsyncWebCrawler(config=browser) as crawler:
                crawler.crawler_strategy.set_hook("before_goto", observe_navigation)
                result = await crawler.arun(url=url, config=config)
                return save_result(result, url, folder, args.keywords)

        report = await asyncio.wait_for(run(), timeout=args.timeout + args.wait + 20)
    except ModuleNotFoundError as exc:
        report.update(reason="dependency_missing", error=str(exc))
    except asyncio.TimeoutError:
        report.update(reason="timeout", error="Overall browser diagnostic timed out")
    except Exception as exc:
        # Technical error belongs in diagnostics, not in the medical answer.
        report.update(reason=failure_reason(str(exc)), error_type=type(exc).__name__, error=str(exc)[:1500])
    report.update(
        seconds=round(time.monotonic() - started, 2), browser=args.browser,
        wait_until=args.wait_until, navigation=navigation,
        collected_at=datetime.now(timezone.utc).isoformat(), output=str(folder.resolve()),
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        missing = self.path == "/missing"
        html = """<!doctype html><html><head><meta charset="utf-8"><title>Crawl4AI local test</title></head>
        <body><nav><a href="https://example.com/departments">科室导航</a></nav>
        <main id="content">This is a local synthetic test, not real hospital information.</main>
        <script>setTimeout(() => {document.querySelector('#content').textContent =
        'DYNAMIC_RENDER_OK 动态正文加载成功。此页面仅用于检查本地浏览器、JavaScript 渲染、正文提取和科室导航链接发现。内容完全虚构，不代表任何医院的真实科室或医疗建议。正常提取后应该看到这个测试标记，以及下方检索需要的业务范围关键词。';}, 200);</script>
        </body></html>"""
        body = ("<html><body>Not found. No hospital information here.</body></html>" if missing else html).encode("utf-8")
        self.send_response(404 if missing else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


async def self_test(args, folder: Path) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        good = await inspect_page(args, root + "/", folder / "dynamic")
        bad = await inspect_page(args, root + "/missing", folder / "missing")
        text_file = folder / "dynamic" / "text.txt"
        dynamic_text = text_file.read_text(encoding="utf-8") if text_file.exists() else ""
        checks = {
            "javascript_rendered": good.get("readable", False) and "DYNAMIC_RENDER_OK" in dynamic_text,
            "department_link_found": any(link["text"] == "科室导航" for link in good.get("department_link_candidates", [])),
            "http_404_rejected": bad.get("http_status") == 404 and bad.get("reason") == "http_error" and not bad["readable"],
        }
        report = {"self_test": True, "passed": all(checks.values()), "checks": checks,
                  "dynamic_result": good, "missing_result": bad}
        (folder / "self_test.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Crawl4AI page reader diagnostic; no LLM or API key required")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url")
    target.add_argument("--self-test", action="store_true")
    parser.add_argument("--browser", choices=["chromium", "msedge", "chrome"], default="chromium")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--screenshot", action="store_true")
    parser.add_argument("--scan", action="store_true", help="Scroll at most 3 steps")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--wait", type=float, default=2)
    parser.add_argument("--wait-until", choices=["domcontentloaded", "commit", "load"],
                        default="domcontentloaded", help="commit returns earlier and may capture incomplete content")
    parser.add_argument("--keywords", nargs="+", default=["科室", "业务范围"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ascii", action="store_true", help="Print JSON with escaped Unicode; saved files remain UTF-8")
    args = parser.parse_args()
    if not 5 <= args.timeout <= 60 or not 0 <= args.wait <= 10:
        parser.error("--timeout must be 5..60 seconds; --wait must be 0..10 seconds")
    try:
        url = public_url(args.url) if args.url else None
    except ValueError as exc:
        parser.error(str(exc))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = args.output or Path(".cache") / "crawl4ai_checks" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    report = asyncio.run(self_test(args, folder) if args.self_test else inspect_page(args, url, folder))
    print(json.dumps(report, ensure_ascii=args.ascii, indent=2))
    return 0 if report.get("passed", report.get("readable", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
