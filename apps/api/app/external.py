"""联网发现官方页面、提取正文，并保留分阶段诊断信息。"""

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import Counter
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .providers import ProviderError
from .schemas import Source
from .source_registry import OFFICIAL_SITES, normalize_host

logger = logging.getLogger(__name__)


class ExternalSearchError(ProviderError):
    """保留原 ProviderError 接口；额外诊断供开发者使用。"""

    def __init__(self, code, message, diagnostics):
        super().__init__(code, message)
        self.diagnostics = diagnostics


class ExternalSearch:
    SEARCH_COUNT = 50
    PAGE_LIMIT = 8
    TIMEOUT = 8
    MAX_REDIRECTS = 3
    MAX_BYTES = 350000

    def __init__(self, settings, transport=None, *, domain_filter=None):
        self.settings = settings
        self.transport = transport
        self.allowed = {
            normalize_host(host) for host in settings.external_domains
        }
        self.domain_filter = (
            os.getenv("EXTERNAL_DOMAIN_FILTER_ENABLED", "true").lower() == "true"
            if domain_filter is None else bool(domain_filter)
        )

    @staticmethod
    def _is_directory(url, title=""):
        path = urlsplit(url).path.lower()
        return bool(
            "department-doctor-search" in path
            or path.rstrip("/") in {"/search", "/sitemap", "/doctor-search"}
            or re.match(r"^(专家团队|医生列表|医师列表|站内搜索|搜索结果)(?:\s|\||$)", title)
        )

    @staticmethod
    def _wants_directory(query):
        return any(word in query for word in (
            "专家团队", "医生名单", "医生列表", "出诊", "坐诊", "排班",
            "预约", "挂号", "找医生", "医生推荐", "科室介绍",
        ))

    def _url_status(self, url, allow_http=False):
        if not isinstance(url, str) or not url.strip():
            return None, "invalid_url"
        try:
            parsed = urlsplit(url)
            host = normalize_host(parsed.hostname or "")
            if not host:
                return None, "invalid_url"
            if parsed.username is not None or parsed.password is not None:
                return None, "url_credentials"
            if host not in self.allowed:
                return None, "disallowed_domain"
            if parsed.scheme == "http" and allow_http:
                if parsed.port not in (None, 80):
                    return None, "invalid_port"
            elif parsed.scheme != "https":
                return None, "non_https"
            elif parsed.port not in (None, 443):
                return None, "invalid_port"
            return host, None
        except ValueError:
            return None, "invalid_url"

    def _host(self, url):
        return self._url_status(url)[0]

    def _candidate(self, url):
        """允许搜索返回 HTTP 链接，但实际下载一律尝试 HTTPS。"""
        host, reason = self._url_status(url, allow_http=True)
        if reason:
            return None, None, reason, False
        parsed = urlsplit(url)
        upgraded = parsed.scheme == "http"
        netloc = parsed.hostname if upgraded else parsed.netloc
        target = urlunsplit(("https", netloc, parsed.path, parsed.query, ""))
        return target, host, None, upgraded

    async def _page_detailed(self, client, url):
        current = url
        visited = set()
        details = {"host": self._host(url), "redirects": 0}

        def failed(reason):
            return None, {**details, "reason": reason}

        try:
            for hop in range(self.MAX_REDIRECTS + 1):
                if not self._host(current):
                    return failed("redirect_blocked")
                if current in visited:
                    return failed("redirect_loop")
                visited.add(current)
                details["host"] = self._host(current)
                async with client.stream("GET", current) as page:
                    details["http_status"] = page.status_code
                    if page.status_code in {301, 302, 303, 307, 308}:
                        location = page.headers.get("location")
                        if not location:
                            return failed("redirect_missing_location")
                        target = urljoin(current, location)
                        # 每一跳都核对域名、HTTPS、端口和用户信息。
                        if not self._host(target):
                            return failed("redirect_blocked")
                        if hop == self.MAX_REDIRECTS:
                            return failed("redirect_limit")
                        details["redirects"] += 1
                        current = target
                        continue
                    if page.status_code != 200:
                        return failed("page_http_error")
                    content_type = page.headers.get("content-type", "").lower()
                    if not any(t in content_type for t in (
                        "text/html", "application/xhtml+xml"
                    )):
                        return failed("non_html")
                    chunks = []
                    total = 0
                    async for chunk in page.aiter_bytes():
                        total += len(chunk)
                        if total > self.MAX_BYTES:
                            return failed("page_too_large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = page.charset_encoding

                # 没有 HTTP charset 时，让解析器读取 HTML 的 meta charset。
                soup = BeautifulSoup(raw, "html.parser", from_encoding=encoding)
                title = (
                    soup.title.get_text(" ", strip=True)
                    if soup.title else "官方资料"
                )
                for tag in soup([
                    "script", "style", "nav", "footer", "header", "form",
                    "noscript",
                ]):
                    tag.decompose()
                block = soup.find("article") or soup.find("main") or soup.body
                if block is None:
                    return failed("body_missing")
                text = block.get_text(" ", strip=True)[:5000]
                details["text_chars"] = len(text)
                if len(text) < 80:
                    return failed("text_too_short")
                source = Source(
                    id="WEB-" + hashlib.sha256(current.encode()).hexdigest()[:12],
                    title=title[:180],
                    institution=OFFICIAL_SITES.get(self._host(current), self._host(current)),
                    url=current,
                    published_at=None,
                    collected_at=date.today().isoformat(),
                    text=text,
                    allowed_departments=[],
                    kind="external_excerpt",
                )
                return source, {**details, "reason": "ok"}
        except httpx.TimeoutException:
            return failed("page_timeout")
        except httpx.HTTPError:
            return failed("page_network_error")
        except (ValueError, LookupError):
            return failed("page_parse_failed")

    async def _page(self, client, url):
        source, _ = await self._page_detailed(client, url)
        return source

    async def search_detailed(self, query="健康科普", top_k=3, *, debug=False):
        diagnostics = {
            "requested_count": self.SEARCH_COUNT,
            "search_http_status": None,
            "raw_count": 0,
            "allowed_count": 0,
            "eligible_count": 0,
            "https_upgraded_count": 0,
            "attempted_pages": 0,
            "extracted_count": 0,
            "returned_count": 0,
            "filtered_reasons": {},
            "page_results": [],
            "scope_mode": "include_requested" if self.domain_filter else "post_filter",
            "scope_domains": sorted(self.allowed) if self.domain_filter else [],
            "candidate_host_counts": {},
        }

        def fail(code, message, reason):
            diagnostics.update(outcome="search_error", failure_reason=reason)
            logger.warning("external_search %s", json.dumps(diagnostics))
            raise ExternalSearchError(code, message, diagnostics) from None

        if not self.settings.bocha_key:
            fail("search_key_missing", "未配置 BOCHA_API_KEY。", "key_missing")
        if not isinstance(query, str) or not query.strip() or len(query) > 400:
            fail("search_query_invalid", "检索词为空或过长。", "query_invalid")
        if not self.allowed:
            fail("external_scope_invalid", "未配置允许检索的官网域名。", "scope_empty")
        if self.domain_filter and len(self.allowed) > 100:
            fail("external_scope_invalid", "单次域名限定最多支持100个域名。", "scope_too_large")
        try:
            top_k = max(1, min(int(top_k), 3))
        except (ValueError, TypeError, OverflowError):
            fail("search_query_invalid", "top_k 应为整数。", "top_k_invalid")

        async with httpx.AsyncClient(
            timeout=self.TIMEOUT,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            body = {
                "query": query.strip(),
                "count": self.SEARCH_COUNT,
                "summary": False,
                "freshness": "noLimit",
            }
            # 在搜索阶段限定官网，避免先得到全网50条再过滤到只剩1条。
            # 后续 URL 核验仍然保留，不信任上游一定遵守 include。
            if self.domain_filter:
                body["include"] = "|".join(sorted(self.allowed))
            try:
                response = await client.post(
                    self.settings.bocha_url,
                    headers={"Authorization": "Bearer " + self.settings.bocha_key},
                    json=body,
                )
                diagnostics["search_http_status"] = response.status_code
                response.raise_for_status()
                data = response.json()
                # HTTP 200 不一定代表上游业务成功。
                if isinstance(data, dict) and data.get("code") not in (None, 200, "200"):
                    fail("external_unavailable", "搜索服务返回业务错误，请检查额度和配置。", "search_business_error")
                items = data["data"]["webPages"]["value"]
                if not isinstance(items, list):
                    raise ValueError("schema")
            except httpx.HTTPStatusError:
                if response.status_code == 400 and self.domain_filter:
                    fail(
                        "external_scope_rejected",
                        "搜索服务拒绝本次请求参数，请联系开发者检查检索配置。",
                        "search_http_400_with_scope",
                    )
                fail("external_unavailable", "搜索接口返回 HTTP 错误，请检查密钥、额度和地址。", "search_http_error")
            except httpx.TimeoutException:
                fail("external_unavailable", "搜索请求超时，请稍后重试。", "search_timeout")
            except httpx.HTTPError:
                fail("external_unavailable", "搜索服务连接失败，请检查网络或代理。", "search_network_error")
            except (ValueError, KeyError, TypeError):
                fail("external_unavailable", "搜索返回格式不符合预期，请检查接口地址。", "search_schema_error")

            diagnostics["raw_count"] = len(items)
            first, rest = [], []
            seen_urls, seen_hosts = set(), set()
            filtered = Counter()
            host_counts = Counter()
            candidates = []
            wants_directory = self._wants_directory(query)
            for item in items[:self.SEARCH_COUNT]:
                if not isinstance(item, dict):
                    filtered["invalid_item"] += 1
                    continue
                raw_url = item.get("url", "")
                try:
                    raw_host = urlsplit(raw_url).hostname if isinstance(raw_url, str) else None
                    host_counts[normalize_host(raw_host or "") or "invalid"] += 1
                except ValueError:
                    host_counts["invalid"] += 1
                title = item.get("name") or item.get("title") or ""
                title = title if isinstance(title, str) else ""
                preview = {"title": title[:180], "url": raw_url if isinstance(raw_url, str) else ""}
                url, host, reason, upgraded = self._candidate(raw_url)
                if reason:
                    filtered[reason] += 1
                    candidates.append({**preview, "reason": reason})
                    continue
                if url in seen_urls:
                    filtered["duplicate_url"] += 1
                    candidates.append({**preview, "reason": "duplicate_url"})
                    continue
                seen_urls.add(url)
                diagnostics["https_upgraded_count"] += int(upgraded)
                if self._is_directory(url, title) and not wants_directory:
                    filtered["directory_page"] += 1
                    candidates.append({**preview, "reason": "directory_page"})
                    continue
                candidates.append({**preview, "reason": "eligible"})
                if host in seen_hosts:
                    rest.append(url)
                else:
                    first.append(url)
                    seen_hosts.add(host)

            diagnostics["filtered_reasons"] = dict(filtered)
            diagnostics["candidate_host_counts"] = dict(host_counts)
            diagnostics["allowed_count"] = len(seen_urls)
            diagnostics["eligible_count"] = len(first) + len(rest)
            targets = (first + rest)[:self.PAGE_LIMIT]
            diagnostics["attempted_pages"] = len(targets)

            async def fetch_page(url):
                try:
                    source, details = await asyncio.wait_for(
                        self._page_detailed(client, url), timeout=self.TIMEOUT
                    )
                    if source is not None and not wants_directory and self._is_directory(source.url, source.title):
                        return None, {**details, "reason": "directory_page"}
                    return source, details
                except asyncio.TimeoutError:
                    return None, {"host": self._host(url), "reason": "page_timeout"}

            pages = await asyncio.gather(*(fetch_page(url) for url in targets))

        unique, extra = [], []
        used_hosts, used_urls = set(), set()
        for source, details in pages:
            diagnostics["page_results"].append(details)
            if source is None or source.url in used_urls:
                continue
            used_urls.add(source.url)
            host = self._host(source.url)
            if host in used_hosts:
                extra.append(source)
            else:
                unique.append(source)
                used_hosts.add(host)

        sources = (unique + extra)[:top_k]
        diagnostics["extracted_count"] = len(used_urls)
        diagnostics["returned_count"] = len(sources)
        diagnostics["outcome"] = (
            "ok" if sources else
            "search_empty" if not diagnostics["raw_count"] else
            "allowlist_empty" if not diagnostics["allowed_count"] else
            "candidate_filtered_empty" if not diagnostics["eligible_count"] else
            "page_fetch_empty"
        )
        # 不记录密钥、原始查询、患者历史、上游错误正文或网页正文。
        logger.info("external_search %s", json.dumps(diagnostics))
        result = {"sources": sources, "diagnostics": diagnostics}
        if debug:
            # 仅显式开发检查返回候选链接，聊天调用和运行日志不包含它们。
            result["candidates"] = candidates
        return result

    async def search(self, query="健康科普", top_k=3):
        result = await self.search_detailed(query, top_k)
        return result["sources"]

    async def discover(self, query, limit=10):
        """只发现候选；让 LLM 选择，绝不在这里下载排名前三的正文。"""
        if not self.settings.bocha_key:
            raise ProviderError("search_key_missing", "未配置 BOCHA_API_KEY。")
        if not isinstance(query, str) or not query.strip() or len(query) > 400:
            raise ProviderError("search_query_invalid", "检索词为空或过长。")
        if not self.allowed or (self.domain_filter and len(self.allowed) > 100):
            raise ProviderError("external_scope_invalid", "请检查允许检索的官网域名。")
        if type(limit) is not int or not 1 <= limit <= 12:
            raise ProviderError("search_query_invalid", "候选数量应为1到12的整数。")
        body = {"query": query.strip(), "count": self.SEARCH_COUNT,
                "summary": False, "freshness": "noLimit"}
        if self.domain_filter:
            body["include"] = "|".join(sorted(self.allowed))
        status = None
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT, transport=self.transport,
                                         follow_redirects=False) as client:
                response = await client.post(
                    self.settings.bocha_url,
                    headers={"Authorization": "Bearer " + self.settings.bocha_key},
                    json=body,
                )
            status = response.status_code
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("code") not in (None, 200, "200"):
                raise ValueError("business error")
            items = data["data"]["webPages"]["value"]
            if not isinstance(items, list):
                raise ValueError("schema")
        except httpx.TimeoutException:
            raise ExternalSearchError("search_timeout", "搜索请求超时。",
                                      {"retryable": True}) from None
        except httpx.HTTPStatusError:
            raise ExternalSearchError(
                "search_http_error", "搜索接口错误，请检查密钥、额度或请求参数。",
                {"http_status": status, "retryable": status in {429, 500, 502, 503, 504}},
            ) from None
        except httpx.HTTPError:
            raise ExternalSearchError("search_network_error", "搜索服务连接失败。",
                                      {"retryable": True}) from None
        except (ValueError, KeyError, TypeError):
            raise ProviderError("search_schema_error", "搜索返回格式或业务状态异常。") from None

        candidates = []
        seen = set()
        filtered = Counter()
        wants_directory = self._wants_directory(query)
        for item in items[:self.SEARCH_COUNT]:
            if not isinstance(item, dict):
                filtered["invalid_item"] += 1
                continue
            url, host, reason, _ = self._candidate(item.get("url", ""))
            title = item.get("name") or item.get("title") or "官方资料"
            title = title if isinstance(title, str) else "官方资料"
            if reason or url in seen or (not wants_directory and self._is_directory(url, title)):
                filtered[reason or ("duplicate_url" if url in seen else "directory_page")] += 1
                continue
            seen.add(url)
            snippet = item.get("snippet") or item.get("summary") or ""
            candidates.append({
                "url": url, "title": title[:180],
                "institution": OFFICIAL_SITES.get(host, host),
                "snippet": snippet[:600] if isinstance(snippet, str) else "",
            })
        diagnostics = {
            "raw_count": len(items), "eligible_count": len(candidates),
            "returned_count": min(limit, len(candidates)), "filtered_reasons": dict(filtered),
            "outcome": "ok" if candidates else "no_eligible_candidates",
        }
        logger.info("external_discover %s", json.dumps(diagnostics))
        return candidates[:limit], diagnostics

    async def read_candidate(self, url, *, allow_directory=False):
        """仅供候选账本调用；仍对实际 URL 和每一跳重定向做核验。"""
        if not self._host(url):
            return None, {"reason": "url_blocked"}
        async with httpx.AsyncClient(timeout=self.TIMEOUT, transport=self.transport,
                                     follow_redirects=False) as client:
            try:
                source, details = await asyncio.wait_for(
                    self._page_detailed(client, url), timeout=self.TIMEOUT
                )
            except asyncio.TimeoutError:
                return None, {"reason": "page_timeout"}
        if source and not allow_directory and self._is_directory(source.url, source.title):
            return None, {**details, "reason": "directory_page"}
        return source, details
