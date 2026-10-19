"""公共候选 URL 校验、来源分类、DNS/IP 固定及有界下载。"""

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class AccessError(Exception):
    def __init__(self, code, *, retryable=False, status=None):
        self.code, self.retryable, self.status = code, retryable, status
        super().__init__(code)


def clean_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise AccessError("invalid_url")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").encode("idna").decode().lower()
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            raise AccessError("invalid_url")
        if parts.port not in (None, 80 if parts.scheme == "http" else 443):
            raise AccessError("invalid_port")
        if "." not in host or host.endswith((".localhost", ".local", ".internal")):
            raise AccessError("non_public_host")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise AccessError("literal_ip_blocked")
        # 尊重搜索/实际链接给出的协议；旧医院网站可能只有 HTTP。
        # 不将 HTTPS 降级，也不把 HTTP 强行改成站点未提供的 HTTPS。
        return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError):
        raise AccessError("invalid_url") from None


def host_of(url):
    host = urlsplit(clean_url(url)).hostname
    return host[4:] if host.startswith("www.") else host


class SourcePolicy:
    def __init__(self, settings):
        configured = list(getattr(settings, "external_domains", ()))
        configured += os.getenv("WEB_VERIFIED_DOMAINS", "").replace("|", ",").split(",")
        self.registered = set()
        for value in configured:
            if value.strip():
                self.registered.add(host_of("https://" + value.strip()))
        self.government_linked = {}

    def classify(self, url):
        host = host_of(url)
        if host.endswith(".gov.cn") or host == "gov.cn":
            return "government"
        if host in self.registered:
            return "registered"
        if host in self.government_linked:
            return "government_linked"
        return "unverified"

    def may_read(self, url):
        # Access to a public document and trust in its publisher are separate.
        # Reading never upgrades authority; DNS/IP/TLS checks still run in the reader.
        public_read = os.getenv("WEB_PUBLIC_READ_ENABLED", "true").lower() in {"true", "1", "yes"}
        return self.classify(url) != "unverified" or public_read


async def resolve_public(host):
    records = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    return sorted({record[4][0] for record in records})


async def checked_addresses(url, resolver=resolve_public):
    host = urlsplit(clean_url(url)).hostname
    try:
        addresses = await resolver(host)
    except (OSError, asyncio.TimeoutError):
        raise AccessError("dns_failed", retryable=True) from None
    if not addresses:
        raise AccessError("dns_empty", retryable=True)
    try:
        if any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise AccessError("private_address_blocked")
    except ValueError:
        raise AccessError("dns_invalid") from None
    return addresses


class ClosingStream(httpx.AsyncByteStream):
    def __init__(self, stream, transport):
        self.stream, self.transport = stream, transport

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            await self.transport.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    """不二次解析域名；每个请求独立连接，避免跨域复用 IP 连接。"""
    def __init__(self, resolver=resolve_public):
        self.resolver = resolver

    async def handle_async_request(self, request):
        addresses = await checked_addresses(str(request.url), self.resolver)
        extensions = {**request.extensions, "sni_hostname": request.url.host}
        headers = request.headers.copy()
        headers["Host"] = request.url.host
        pinned = httpx.Request(request.method, request.url.copy_with(host=addresses[0]),
                               headers=headers, stream=request.stream, extensions=extensions)
        transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        try:
            response = await transport.handle_async_request(pinned)
        except BaseException:
            await transport.aclose()
            raise
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=ClosingStream(response.stream, transport),
                              extensions=response.extensions)


@dataclass
class Download:
    url: str
    content: bytes
    content_type: str


class SafeFetcher:
    MAX_BYTES = 2_000_000
    TIMEOUT = 8

    def __init__(self, policy, transport=None, resolver=resolve_public):
        self.policy, self.transport, self.resolver = policy, transport, resolver

    async def fetch(self, url):
        try:
            return await asyncio.wait_for(self._fetch(url), self.TIMEOUT)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise AccessError("page_timeout", retryable=True) from None
        except httpx.HTTPError:
            raise AccessError("page_network_error", retryable=True) from None

    async def _fetch(self, url):
        current, seen = clean_url(url), set()
        transport = self.transport or PinnedTransport(self.resolver)
        async with httpx.AsyncClient(transport=transport, timeout=self.TIMEOUT,
                                     follow_redirects=False, trust_env=False) as client:
            for hop in range(4):
                if current in seen:
                    raise AccessError("redirect_loop")
                seen.add(current)
                if not self.policy.may_read(current):
                    raise AccessError("source_unverified")
                if self.transport is not None:
                    # 注入 MockTransport 时同样验证地址；真正请求由 PinnedTransport 固定 IP。
                    await checked_addresses(current, self.resolver)
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or hop == 3:
                            raise AccessError("redirect_limit")
                        current = clean_url(urljoin(current, location))
                        continue
                    if response.status_code != 200:
                        raise AccessError("page_http_error", status=response.status_code,
                                          retryable=response.status_code in {429, 500, 502, 503, 504})
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(t in content_type for t in ("text/html", "application/xhtml+xml", "application/pdf")):
                        raise AccessError("unsupported_document")
                    content, total = [], 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.MAX_BYTES:
                            raise AccessError("page_too_large")
                        content.append(chunk)
                    return Download(current, b"".join(content), content_type)
        raise AccessError("redirect_limit")
