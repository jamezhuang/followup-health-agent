"""搜索与正文服务适配器；不对患者内容、密钥或上游正文做日志输出。"""

import os
import re
from collections import Counter
from urllib.parse import urlsplit

import httpx

from .web_access import AccessError, checked_addresses, clean_url, host_of, resolve_public
from .tencent_search import TencentSearchFailure, search_tencent


class ProviderFailure(Exception):
    def __init__(self, code, *, retryable=False, status=None):
        self.code, self.retryable, self.status = code, retryable, status
        super().__init__(code)


def prioritize_publisher_candidates(query, candidates):
    """Rank the full discovery pool before truncation; never upgrade authority."""
    if not any(word in query for word in ('官网','官方网站')):return candidates
    from .followup.hospital_planner import _names
    from urllib.parse import urlsplit
    import re
    names=_names(query)
    if not names:return candidates
    def score(item):
        title=re.sub(r'[\s（）()]','',item.get('title',''))
        points=0
        for name in names:
            if name not in title:continue
            if title.endswith(name):points=max(points,8)
            if title.endswith(name) and title.count(name)>1:points=max(points,24)
            if title==name:points=max(points,16)
            if title.endswith(name) and any(word in title for word in ('联系我们','来院交通','交通指南','网站改版')):points=max(points,28)
        if points and len(urlsplit(item.get('url','')).path.strip('/').split('/'))<=1:points+=2
        return points
    ranked=sorted(candidates,key=lambda item:(item.get('authority') not in {'government','registered','government_linked'},-score(item)))
    return [{**item,'publisher_discovery_score':score(item)} for item in ranked]


class WebProviders:
    @staticmethod
    def forbidden_reason(response):
        """Classify explicit permanent failures without retaining upstream text."""
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        message = ' '.join(str(payload.get(key, ''))[:300] for key in ('code', 'message', 'msg', 'error'))
        if re.search(r'余额不足|额度不足|欠费|套餐.*(?:到期|耗尽)|insufficient.?balance|quota.?exceeded|credit.?exhausted', message, re.I):
            return 'search_quota_error'
        if re.search(r'(?:invalid|expired|missing).{0,20}(?:api.?key|token|credential)|(?:api.?key|token|credential).{0,20}(?:invalid|expired)|密钥.*(?:无效|过期)|未授权|无权限|permission.?denied|unauthorized', message, re.I):
            return 'search_auth_error'
        return 'search_access_refused'

    def __init__(self, settings, policy, transport=None, resolver=resolve_public):
        self.settings, self.policy = settings, policy
        self.transport, self.resolver = transport, resolver
        self.baidu_key = os.getenv("BAIDU_SEARCH_API_KEY", "").strip()
        self.tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
        # Operator-controlled endpoint, never supplied by model/user input. Loopback
        # is intentional for a self-hosted engine; result URLs still use clean_url.
        self.searxng_url = os.getenv('SEARXNG_URL', '').strip().rstrip('/')
        if self.searxng_url:
            parts = urlsplit(self.searxng_url)
            if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError('SEARXNG_URL must be an operator-controlled HTTP(S) base URL')
        self.tencent_enabled = bool(getattr(settings, 'tencent_search_enabled', False)
                                    and getattr(settings, 'tencent_secret_id', '')
                                    and getattr(settings, 'tencent_secret_key', ''))
        # 国内模式确保即使环境中残留其他服务密钥，也不会调用该服务。
        self.domestic_only = os.getenv("WEB_DOMESTIC_ONLY", "false").strip().lower() == "true"
        if self.domestic_only:
            self.tavily_key = ""
            # Self-hosting does not guarantee domestic upstreams. Require an
            # operator declaration after selecting the engine configuration.
            if os.getenv('SEARXNG_DOMESTIC_ONLY', 'false').lower() != 'true':
                self.searxng_url = ''
        self.primary = os.getenv("WEB_SEARCH_PROVIDER", "baidu").strip().lower()
        if self.primary not in {"baidu", "bocha", "tencent", "searxng", "tavily"}:
            raise ValueError("WEB_SEARCH_PROVIDER must be baidu, bocha, tencent, searxng or tavily")
        if self.domestic_only and self.primary == "tavily":
            self.primary = "bocha"

    def configured(self, provider):
        return bool({"baidu":self.baidu_key, "bocha":getattr(self.settings,"bocha_key",""),
                     "tencent":self.tencent_enabled, "searxng":self.searxng_url,
                     "tavily":self.tavily_key}.get(provider,""))

    def available(self):
        return [name for name in ("baidu", "bocha", "tencent", "searxng", "tavily") if self.configured(name)]

    async def searxng_search(self, query):
        try:
            async with httpx.AsyncClient(timeout=8, transport=self.transport, follow_redirects=False) as client:
                response = await client.get(self.searxng_url + '/search', params={
                    'q': query, 'format': 'json', 'language': 'zh-CN', 'categories': 'general'})
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                    raise ValueError('schema')
                # Engine errors with zero hits are an outage, not genuine no-results.
                if not data['results'] and data.get('unresponsive_engines'):
                    raise ProviderFailure('search_service_unavailable', retryable=True)
                return data['results']
        except httpx.HTTPStatusError as error:
            raise ProviderFailure('search_http_error', status=error.response.status_code,
                                  retryable=error.response.status_code in {429, 500, 502, 503, 504}) from None
        except httpx.TimeoutException:
            raise ProviderFailure('search_timeout', retryable=True) from None
        except httpx.HTTPError:
            raise ProviderFailure('search_network_error', retryable=True) from None
        except (TypeError, ValueError):
            raise ProviderFailure('search_schema_error') from None

    async def post(self, url, key, body, *, auth_header="Authorization"):
        if not key:
            raise ProviderFailure("search_key_missing")
        try:
            async with httpx.AsyncClient(timeout=8, transport=self.transport,
                                         follow_redirects=False) as client:
                response = await client.post(url, headers={auth_header: "Bearer " + key}, json=body)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("schema")
                return result
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 403:
                raise ProviderFailure(self.forbidden_reason(error.response), status=status) from None
            raise ProviderFailure("search_http_error", status=status,
                                  retryable=status in {429, 500, 502, 503, 504}) from None
        except httpx.TimeoutException:
            raise ProviderFailure("search_timeout", retryable=True) from None
        except httpx.HTTPError:
            raise ProviderFailure("search_network_error", retryable=True) from None
        except (TypeError, ValueError):
            raise ProviderFailure("search_schema_error") from None

    async def one_search(self, provider, query, scope):
        if provider == "baidu":
            # Official web_search endpoint: retrieve candidates, not an AI-generated answer.
            data = await self.post("https://qianfan.baidubce.com/v2/ai_search/web_search", self.baidu_key,
                {"messages":[{"role":"user","content":query}], "search_source":"baidu_search_v2",
                 "resource_type_filter":[{"type":"web","top_k":20}]},
                auth_header="X-Appbuilder-Authorization")
            if data.get("code") not in (None, 0, "0", 200, "200"):
                raise ProviderFailure("search_business_error")
            items = data.get("references")
        elif provider == "bocha":
            body = {"query": query, "count": 50, "summary": False, "freshness": "noLimit"}
            if scope == "trusted" and self.policy.registered:
                body["include"] = "|".join(sorted(self.policy.registered)[:100])
            data = await self.post(self.settings.bocha_url, self.settings.bocha_key, body)
            if data.get("code") not in (None, 200, "200"):
                raise ProviderFailure("search_business_error")
            try:
                items = data["data"]["webPages"]["value"]
            except (KeyError, TypeError):
                raise ProviderFailure("search_schema_error") from None
        elif provider == 'tencent':
            try:
                items = await search_tencent(self.settings, query)
            except TencentSearchFailure as error:
                raise ProviderFailure(error.code, retryable=error.retryable) from None
        elif provider == 'searxng':
            items = await self.searxng_search(query)
        else:
            body = {"query": query, "max_results": 20, "search_depth": "advanced",
                    "include_answer": False, "include_raw_content": False}
            if scope == "trusted" and self.policy.registered:
                body["include_domains"] = sorted(self.policy.registered)
            data = await self.post("https://api.tavily.com/search", self.tavily_key, body)
            items = data.get("results")
        if not isinstance(items, list):
            raise ProviderFailure("search_schema_error")
        seen, candidates, filtered = set(), [], Counter()
        for item in items[:50]:
            if not isinstance(item, dict):
                filtered["invalid_item"] += 1
                continue
            try:
                url = clean_url(item.get("url", ""))
            except AccessError as error:
                filtered[error.code] += 1
                continue
            if url in seen:
                filtered["duplicate_url"] += 1
                continue
            seen.add(url)
            authority = self.policy.classify(url)
            if scope == "trusted" and authority == "unverified":
                filtered["outside_trusted_scope"] += 1
                continue
            title = item.get("name") or item.get("title") or "网页候选"
            snippet = item.get("snippet") or item.get("content") or item.get("summary") or ""
            candidates.append({"url": url, "title": title[:180] if isinstance(title, str) else "网页候选",
                               "snippet": snippet[:500] if isinstance(snippet, str) else "",
                               "institution": host_of(url), "authority": authority,
                               "provider": provider})
        # 保持各可信度分组内的搜索排序；公共发现不丢弃未知网站。
        candidates.sort(key=lambda item: item["authority"] == "unverified")
        return candidates, {"provider": provider, "raw_count": len(items),
                            "candidate_count": len(candidates), "filtered_reasons": dict(filtered)}

    async def search(self, query, limit, provider="auto", scope="public"):
        available = self.available()
        if provider != "auto" and provider not in available:
            return {"ok": False, "candidates": [], "provider_attempts": [],
                    "available_providers": available, "outcome": "search_failed",
                    "error": "provider_not_configured", "retryable": False}
        ordered = list(dict.fromkeys([self.primary, "baidu", "bocha", "tencent", "searxng", "tavily"]))
        providers = [provider] if provider != "auto" else [name for name in ordered if name in available]
        attempts, combined, seen = [], [], set()
        # One deferred recovery attempt for an unexplained HTTP 403, after other
        # configured engines. Known credential/quota errors must not be retried.
        # The recovery set prevents recovery requests from scheduling themselves.
        recovery = set()
        for name in providers:
            if not self.configured(name):
                attempts.append({"provider": name, "error": "search_key_missing", "retryable": False})
                continue
            try:
                candidates, details = await self.one_search(name, query, scope)
            except ProviderFailure as error:
                attempts.append({"provider": name, "error": error.code,
                                 "retryable": error.retryable, "http_status": error.status})
                if error.code == 'search_access_refused' and name not in recovery:
                    recovery.add(name)
                    providers.append(name)
                continue
            attempts.append(details)
            for candidate in candidates:
                if candidate['url'] not in seen:
                    combined.append(candidate)
                    seen.add(candidate['url'])
            # An engine returning one hit should not prevent configured backups
            # from filling a broad request. Explicit provider selection stays exact.
            if combined and (provider != 'auto' or len(combined) >= min(limit, 4)):
                return {"ok": True, "candidates": prioritize_publisher_candidates(query,combined)[:limit], "provider_attempts": attempts,
                        "available_providers": available, "outcome": "ok"}
        if combined:
            return {"ok": True, "candidates": prioritize_publisher_candidates(query,combined)[:limit], "provider_attempts": attempts,
                    "available_providers": available, "outcome": "ok"}
        # 成功但空与接口全失败区分，供模型换词或换工具。
        any_success = any("raw_count" in row for row in attempts)
        return {"ok": any_success, "candidates": [], "provider_attempts": attempts,
                "available_providers": available,
                "outcome": "search_empty" if any_success else "search_failed",
                "error": None if any_success else "all_search_providers_failed",
                "retryable": any(row.get("retryable", False) for row in attempts)}

    async def extract(self, url, query=""):
        if not self.tavily_key:
            raise ProviderFailure("extract_key_missing")
        await checked_addresses(url, self.resolver)
        data = await self.post("https://api.tavily.com/extract", self.tavily_key, {
            "urls": [url], "extract_depth": "advanced", "format": "markdown",
            # 不使用服务端query切片，避免 [...] 拼接混入逐字引用；本地按query选片段。
            "include_images": False, "timeout": 6,
        })
        results = data.get("results") or []
        if not isinstance(results, list):
            raise ProviderFailure("extract_schema_error")
        for item in results:
            if not isinstance(item, dict):
                continue
            final_url = clean_url(item.get("url", ""))
            # 托管服务内部重定向不可逐跳观察；只接受同一主机的已核验输出。
            if host_of(final_url) != host_of(url) or not self.policy.may_read(final_url):
                raise ProviderFailure("extract_source_changed")
            text = item.get("raw_content")
            if isinstance(text, str) and len(text.strip()) >= 40:
                return {"url": final_url, "text": text[:80_000], "title": "网页提取正文",
                        "links": [], "document_type": "hosted_extract"}
        raise ProviderFailure("extract_empty")
