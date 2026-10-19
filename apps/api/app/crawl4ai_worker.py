"""Single-page Crawl4AI worker; stdin/stdout JSON; launched by browser_reader."""
import asyncio
import contextlib
import io
import ipaddress
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
from app.web_access import AccessError, checked_addresses, clean_url, host_of
from app.web_documents import parse_html


async def pin_hosts(url):
    """Finite host map: retain browser networking without a second DNS resolution."""
    host = urlsplit(url).hostname
    addresses = await checked_addresses(url)
    address = sorted(addresses, key=lambda item: (ipaddress.ip_address(item).version, item))[0]
    pinned = {host: address}
    # Permit only the corresponding www/non-www alias, not arbitrary subdomains.
    alias = host[4:] if host.startswith("www.") else "www." + host
    try:
        alias_addresses = await checked_addresses("https://" + alias + "/")
        pinned[alias] = sorted(alias_addresses, key=lambda item: (ipaddress.ip_address(item).version, item))[0]
    except AccessError:
        pass
    return pinned


def resolver_rules(pinned):
    rows = []
    for host, address in pinned.items():
        if ipaddress.ip_address(address).version == 6:
            address = "[" + address + "]"
        rows.append(f"MAP {host} {address}")
    return ", ".join([*rows, "MAP * ~NOTFOUND"])


async def crawl(payload):
    os.environ["WEB_HTTP_ALLOWED_HOSTS"] = payload.get("http_hosts", "")
    url = clean_url(payload["url"])
    browser_name = payload.get("browser", "msedge")
    if browser_name not in {"chromium", "chrome", "msedge"}:
        raise AccessError("browser_config_invalid")
    pinned = await asyncio.wait_for(pin_hosts(url), 8)
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    from playwright.async_api import Browser

    # Crawl4AI 0.9.4 does not expose this context option through BrowserConfig.
    # Scope the factory wrapper to this isolated worker, including its default context.
    if not getattr(Browser.new_context, "_blocks_service_workers", False):
        original_context = Browser.new_context
        async def blocked_context(self, **options):
            options["service_workers"] = "block"
            return await original_context(self, **options)
        blocked_context._blocks_service_workers = True
        Browser.new_context = blocked_context

    state = {"requests": 0, "navigations": 0, "blocked_resources": 0,
             "allowed_resources": 0, "resource_budget_exhausted": False, "blocked_reasons": {},
             "domcontentloaded": False, "main_response": None, "fatal": None}

    async def setup(page, context, **_):
        async def guard(route):
            request = route.request
            state["requests"] += 1
            main_navigation = request.is_navigation_request() and request.frame == page.main_frame
            try:
                request_url = clean_url(request.url)
                parts = urlsplit(request_url)
                if main_navigation and parts.hostname not in pinned:
                    raise AccessError("source_unverified")
                if parts.hostname not in pinned or request.method != "GET":
                    raise AccessError("browser_resource_blocked")
                # Do not silently convert a page's subresource HTTP request to HTTPS.
                if parts.scheme != urlsplit(request.url).scheme:
                    raise AccessError("browser_resource_blocked")
                if request.resource_type in {"image", "font", "media", "websocket"}:
                    raise AccessError("browser_resource_blocked")
                if request.is_navigation_request():
                    if request.frame != page.main_frame:
                        raise AccessError("browser_resource_blocked")
                    state["navigations"] += 1
                    if state["navigations"] > 4:
                        raise AccessError("redirect_limit")
                    if host_of(request_url) != host_of(url):
                        raise AccessError("source_unverified")
                else:
                    # Count permitted subresources, not already-blocked images/trackers.
                    # Exhaustion stops extra downloads, not extraction of the loaded document.
                    if state["allowed_resources"] >= 100:
                        state["resource_budget_exhausted"] = True
                        raise AccessError("browser_resource_budget")
                    state["allowed_resources"] += 1
                await route.continue_()
            except AccessError as error:
                state["blocked_resources"] += 1
                state["blocked_reasons"][error.code] = state["blocked_reasons"].get(error.code, 0) + 1
                if error.code in {"redirect_limit", "source_unverified"} or (
                    main_navigation and error.code in {"invalid_url", "invalid_port", "non_public_host", "literal_ip_blocked"}
                ):
                    state["fatal"] = error.code
                await route.abort()

        # Context route covers ordinary requests and popup pages. Unlisted hosts
        # also fail DNS because of the finite browser host mapping.
        await context.route("**/*", guard)
        if hasattr(context, "route_web_socket"):
            async def block_websocket(websocket):
                await websocket.close()
            await context.route_web_socket("**/*", block_websocket)
        page.on("domcontentloaded", lambda: state.update(domcontentloaded=True))

        def response_received(response):
            if response.request.is_navigation_request() and response.request.frame == page.main_frame:
                state["main_response"] = {"url": response.url, "status": response.status}
        page.on("response", response_received)
        return page

    browser = BrowserConfig(
        browser_type="chromium", channel=browser_name, chrome_channel=browser_name,
        headless=True, ignore_https_errors=False, accept_downloads=False,
        # text_mode in this pinned version adds --disable-javascript; block images in the route instead.
        text_mode=False, light_mode=True, verbose=False,
        extra_args=["--host-resolver-rules=" + resolver_rules(pinned), "--no-proxy-server",
                    "--disable-quic", "--disable-background-networking",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
    )
    config = CrawlerRunConfig(cache_mode=CacheMode.DISABLED, wait_until="commit", page_timeout=30000,
                              delay_before_return_html=5, body_visibility_timeout=5000,
                              word_count_threshold=1, scan_full_page=False, screenshot=False, pdf=False,
                              verbose=False)
    crawler = AsyncWebCrawler(config=browser)
    # This version includes certificate-ignore flags even when ignore_https_errors=False.
    # Remove those flags before launch in this dedicated worker.
    manager = crawler.crawler_strategy.browser_manager
    original_args = manager._build_browser_args
    def secure_args():
        options = original_args()
        options["args"] = [arg for arg in options["args"] if not arg.startswith((
            "--ignore-certificate-errors", "--allow-running-insecure-content", "--disable-web-security",
        ))]
        return options
    manager._build_browser_args = secure_args
    async with crawler:
        crawler.crawler_strategy.set_hook("before_goto", setup)
        result = await crawler.arun(url=url, config=config)
    if state["fatal"]:
        raise AccessError(state["fatal"])
    status = result.redirected_status_code or result.status_code
    if status is None or status != 200:
        raise AccessError("browser_http_error" if status else "browser_navigation_failed", status=status,
                          retryable=status in {None, 429, 500, 502, 503, 504})
    if not result.success:
        error = (result.error_message or "").lower()
        raise AccessError("browser_timeout" if "timeout" in error else "browser_content_failed",
                          retryable="timeout" in error)
    final_url = clean_url(result.redirected_url or result.url)
    if urlsplit(final_url).hostname not in pinned or host_of(final_url) != host_of(url):
        raise AccessError("source_unverified")
    html = result.html or ""
    content = html.encode("utf-8")
    if len(content) > 2_000_000:
        raise AccessError("page_too_large")
    # Browser HTML is Unicode already; passing bytes could reapply an old GBK meta charset.
    page = parse_html(SimpleNamespace(url=final_url, content=html))
    page.update(reader="crawl4ai", http_status=status, browser_diagnostics={
        "domcontentloaded": state["domcontentloaded"], "main_response": state["main_response"],
        "request_count": state["requests"], "blocked_resources": state["blocked_resources"],
        "allowed_resources": state["allowed_resources"],
        "resource_budget_exhausted": state["resource_budget_exhausted"],
        "blocked_reasons": state["blocked_reasons"],
    })
    return {"ok": True, "page": page}


def main():
    try:
        raw = sys.stdin.buffer.read(20001)
        if len(raw) > 20000:
            raise AccessError("browser_input_too_large")
        payload = json.loads(raw.decode("utf-8"))
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        # Third-party logs never become tool JSON or patient-facing output.
        with contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(crawl(payload))
    except AccessError as error:
        result = {"ok": False, "error": error.code, "retryable": error.retryable, "http_status": error.status}
    except ModuleNotFoundError:
        result = {"ok": False, "error": "browser_dependency_missing", "retryable": False}
    except Exception as error:
        code = "browser_missing" if "executable" in str(error).lower() else "browser_worker_failed"
        result = {"ok": False, "error": code, "retryable": False}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
