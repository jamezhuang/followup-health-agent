"""本轮公共候选账本：发现、核验、读取、链接发现；不自动写入正式知识库。"""
# CAREBRIEF_RELIABILITY_V1
from .agent_reliability import extract_published_date, budget_aware_reader

import asyncio
import hashlib
from datetime import date
from typing import Literal

from pydantic import Field, ValidationError, field_validator

from .schemas import Source, StrictModel
from .web_access import AccessError, SafeFetcher, SourcePolicy, clean_url, host_of, resolve_public
from .web_documents import excerpt, directory_excerpt, parse_html, parse_pdf
from .web_providers import ProviderFailure, WebProviders
from .browser_reader import Crawl4AIReader
from .followup.checkpoints import checkpoint_tool


class WebSearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=400)
    limit: int = Field(default=5, ge=1, le=12)
    scope: Literal["public", "trusted"] = "public"
    provider: Literal["auto", "baidu", "bocha", "tencent", "searxng", "tavily"] = "auto"

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("blank")
        return value.strip()


class CandidateArgs(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=80)


class ReadPageArgs(CandidateArgs):
    query: str = Field(default="", max_length=400)
    method: Literal["auto", "direct", "extract", "browser"] = "auto"
    purpose: Literal["reference", "hospital_directory"] = "reference"


class VerifyArgs(CandidateArgs):
    parent_candidate_id: str = Field(default="", max_length=80)


class MapArgs(CandidateArgs):
    query: str = Field(default="", max_length=400)
    limit: int = Field(default=10, ge=1, le=12)
    method: Literal["auto", "direct", "browser"] = "auto"


def definition(name, description, schema):
    return {"type": "function", "function": {"name": name, "description": description,
                                              "parameters": schema.model_json_schema()}}


def failure(error):
    return {"ok": False, "error": error.code, "retryable": error.retryable,
            "http_status": error.status,
            "reader_attempts": getattr(error, "reader_attempts", []),
            "hint": "修正参数、换词、换来源或其他可用工具；错误不是事实证据。"}


class WebTools:
    def __init__(self, external, collected, notices, *, policy=None, providers=None, fetcher=None, browser=None):
        self.external, self.collected, self.notices = external, collected, notices
        self.policy = policy or SourcePolicy(external.settings)
        self.providers = providers or WebProviders(external.settings, self.policy,
                                                   transport=getattr(external, "transport", None),
                                                   resolver=getattr(external, "resolver", resolve_public))
        self.fetcher = fetcher or SafeFetcher(self.policy,
                                              transport=getattr(external, "transport", None),
                                              resolver=getattr(external, "resolver", resolve_public))
        self.candidates, self.pages = {}, {}
        self.browser = browser or Crawl4AIReader(self.policy)
        self.search_attempted = False

    @property
    def definitions(self):
        available = self.providers.available()
        methods = ["auto", "direct"]
        read_help = "method=auto先直接读取；direct只使用普通HTTP读取。"
        if self.browser.enabled:
            methods.append("browser")
            read_help += ("适用的读取失败自动尝试本地Crawl4AI；也可指定browser。"
                          "如果直接读取只有导航或缺少正文，可调用browser再读。"
                          "需要详情时可先map_website，再读实际详情链接；浏览器次数受运行预算限制。")
        if self.providers.tavily_key:
            methods.append("extract")
            read_help += "extract使用已配置的Tavily正文提取。"
        definitions = [
            definition("search_web", "发现公共网页候选，不限于已入库医院。默认scope=public；"
                       "trusted仅限定已登记网站。返回标题、摘要、来源分类；候选摘要不可引用。"
                       "默认返回前5条候选，按问题选择相关页面；不把排名当成权威性或医生水平。"
                       "可查询医院名单、等级、科室、医生、就诊流程和公共健康信息。"
                       "查询不含用户身份、联系方式和私人病历。可改词再次搜索。当前已配置搜索服务：" + ("、".join(available) or "无") +
                       "；只调用当前可用服务。", WebSearchArgs),
            definition("verify_source", "记录来源分类和readable状态；未核验不等于无法读取。"
                       "开启公共读取时，未知来源可读取正文，但不能冒充已核验官网。"
                       "未知医院网站可用本轮已读取的政府页面实际医院链接作关联依据，"
                       "提供parent_candidate_id。不能仅凭模型判断提升可信度；来源关联不是医院等级认证。", VerifyArgs),
            definition("read_webpage", "读取本轮搜索或实际链接发现的网页或PDF，query选择相关正文。"
                       "公开来源无需预先登记医院、无需先verify_source，读取成功即可提供带来源的引用。"
                       "purpose保留兼容，两种值均可引用。未独立核验时准确归属为‘该页面记载’。"
                       "页面自述、第三方报道与行政认定分别说明；不能凭搜索排名认证三甲或评选名医。"
                       + read_help + "检查正文覆盖后引用，失败可换页。", ReadPageArgs),
            definition("map_website", "发现指定候选页面上的实际链接，不是无限爬虫。"
                       "可从政府目录、医院首页查找简介、评审公告和PDF附件。"
                       "医院业务查询应优先发现科室导航及具体科室详情。method=auto允许读取兜底；"
                       "已配置浏览器时可指定browser获取渲染后的链接。"
                       "query用于优先选择相关链接；核验结果readable=true时可继续读取，"
                       "不把unverified当作网页不可读。", MapArgs),
        ]
        # schema和说明同步能力；未配置的provider不再出现在模型选择列表。
        definitions[0]["function"]["parameters"]["properties"]["provider"]["enum"] = ["auto", *available]
        definitions[2]["function"]["parameters"]["properties"]["method"]["enum"] = methods
        definitions[3]["function"]["parameters"]["properties"]["method"]["enum"] = [m for m in methods if m != "extract"]
        return definitions

    @property
    def handlers(self):
        return {"search_web": self.search, "verify_source": self.verify,
                "read_webpage": self.read, "map_website": self.map}

    def args(self, schema, raw):
        try:
            return schema.model_validate(raw)
        except ValidationError:
            return None

    def put(self, item, *, parent=""):
        url = clean_url(item["url"])
        candidate_id = "CAND-" + hashlib.sha256(url.encode()).hexdigest()[:16]
        result = {**item, "url": url, "candidate_id": candidate_id,
                  "authority": self.policy.classify(url)}
        if parent:
            result["parent_candidate_id"] = parent
        self.candidates[candidate_id] = result
        return result

    @staticmethod
    def bounded(items, limit=14000):
        import json
        output, size = [], 0
        for item in items:
            cost = len(json.dumps(item, ensure_ascii=False)) + 2
            if size + cost > limit:
                break
            output.append(item)
            size += cost
        return output

    def invalid(self):
        return {"ok": False, "error": "invalid_arguments", "retryable": False,
                "hint": "按此工具schema修正参数，不接受额外字段或任意URL。"}

    def candidate(self, candidate_id):
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise AccessError("unknown_candidate")
        return candidate

    @checkpoint_tool('search_web')
    async def search(self, raw):
        args = self.args(WebSearchArgs, raw)
        if args is None:
            return self.invalid()
        self.search_attempted = True
        result = await self.providers.search(args.query, args.limit, args.provider, args.scope)
        result["candidates"] = self.bounded([self.put(item) for item in result["candidates"]])
        result.update(evidence_ready=False, hint=(
            "候选摘要用于选页，调用read_webpage读取相关正文后引用；不必先verify_source。"
            "unverified不是访问失败，公开页面可读取并按实际来源归属引用。"
            "没有结果可改写查询、改scope=public。只使用available_providers里的服务。"
            "医院名单页失效时，改搜具体医院的官网简介、评审公告；医保定点和三级不等于三甲。"
        ))
        return result

    @checkpoint_tool('verify_source')
    async def verify(self, raw):
        args = self.args(VerifyArgs, raw)
        if args is None:
            return self.invalid()
        try:
            candidate = self.candidate(args.candidate_id)
            authority = self.policy.classify(candidate["url"])
            basis = "registered_or_government_domain"
            if authority == "unverified" and args.parent_candidate_id:
                self.candidate(args.parent_candidate_id)
                page = self.pages.get(args.parent_candidate_id)
                if page is None or self.policy.classify(page["url"]) != "government":
                    raise AccessError("government_page_not_read")
                for link in page["links"]:
                    # 必须是实际政府页面的医院机构链接，而非模型提供的任意网址。
                    if host_of(link["url"]) == host_of(candidate["url"]) and "医院" in link["title"]:
                        self.policy.government_linked[host_of(candidate["url"])] = {
                            "parent_url": page["url"], "anchor": link["title"],
                        }
                        authority, basis = "government_linked", "actual_government_hospital_link"
                        break
            return {"ok": True, "candidate_id": args.candidate_id, "authority": authority,
                    "verified": authority != "unverified", "readable": self.policy.may_read(candidate["url"]),
                    "basis": basis if authority != "unverified" else "no_verified_basis",
                    "hint": "readable与verified不同；可读未核验网页也不能冒充官网。"
                            "来源核验不证明三甲等级或医学结论；仍须读取相关正文。"}
        except AccessError as error:
            return failure(error)

    async def load(self, candidate_id, query="", method="auto"):
        candidate = self.candidate(candidate_id)
        if not self.policy.may_read(candidate["url"]):
            raise AccessError("source_unverified")
        if method == "browser" and not self.browser.enabled:
            raise AccessError("browser_disabled")
        if method != "extract" and candidate_id in self.pages:
            cached_page = self.pages[candidate_id]
            if (method == "auto" or method == "browser" and cached_page["reader"] == "crawl4ai"
                    or method == "direct" and cached_page["reader"] == "direct"):
                return cached_page, True
        if method == "extract" and not self.providers.tavily_key:
            raise ProviderFailure("extract_key_missing")
        attempts = []
        try:
            if method == "extract":
                raise AccessError("extract_requested")
            if method == "browser":
                raise AccessError("browser_requested")
            download = await self.fetcher.fetch(candidate["url"])
            page = await parse_pdf(download) if "application/pdf" in download.content_type else parse_html(download)
            if page["document_type"] == "pdf":
                page["title"] = candidate.get("title") or "官方 PDF 附件"
            page["reader"] = "direct"
            attempts.append({"reader": "direct", "status": "ok"})
        except (AccessError, asyncio.TimeoutError) as error:
            can_fallback = isinstance(error, asyncio.TimeoutError) or error.code in {
                "page_http_error", "page_timeout", "page_network_error", "text_too_short", "extract_requested", "browser_requested",
            }
            if method == "direct" or not can_fallback:
                raise
            attempts.append({"reader": "direct", "status": "skipped" if method in {"browser", "extract"} else "failed",
                             "error": getattr(error, "code", "page_timeout")})
            browser_eligible = not (isinstance(error, AccessError) and error.code == "page_http_error"
                                   and error.status not in {None, 403, 408, 412, 429, 500, 502, 503, 504})
            if method != "extract" and self.browser.enabled and browser_eligible:
                try:
                    page = await self.browser.fetch(candidate["url"])
                    attempts.append({"reader": "crawl4ai", "status": "ok", "seconds": page.get("seconds")})
                except AccessError as browser_error:
                    attempts.append({"reader": "crawl4ai", "status": "failed", "error": browser_error.code})
                    # Address/source refusals must never fall through to another reader.
                    if method == "browser" or not self.providers.tavily_key or browser_error.code in {
                        "source_unverified", "private_address_blocked", "literal_ip_blocked",
                        "non_public_host", "redirect_limit", "browser_resource_budget",
                    }:
                        browser_error.reader_attempts = attempts
                        raise
                    page = await self.providers.extract(candidate["url"], query)
                    page["reader"] = "tavily_extract"
                    attempts.append({"reader": "tavily_extract", "status": "ok"})
            elif self.providers.tavily_key and method != "browser":
                page = await self.providers.extract(candidate["url"], query)
                page["reader"] = "tavily_extract"
                attempts.append({"reader": "tavily_extract", "status": "ok"})
            else:
                error.reader_attempts = attempts
                raise
        page["reader_attempts"] = attempts
        self.pages[candidate_id] = page
        return page, False

    @budget_aware_reader
    @checkpoint_tool('read_webpage')
    async def read(self, raw):
        args = self.args(ReadPageArgs, raw)
        if args is None:
            return self.invalid()
        try:
            page, cached = await self.load(args.candidate_id, args.query, args.method)
            text = directory_excerpt(page["text"],args.query) if args.purpose=='hospital_directory' else excerpt(page["text"], args.query)
            authority = self.policy.classify(page["url"])
            unverified = authority == "unverified"
            # 片段版本进入ID，避免同一页面不同query把之前可用的quote覆盖掉。
            digest = hashlib.sha256((page["url"] + "\n" + text).encode()).hexdigest()[:20]
            institution = host_of(page["url"])
            if unverified:
                institution += "（来源身份未独立核验；引用为该页面记载）"
            source = Source(id="WEB-" + digest, title=page["title"], institution=institution,
                            url=page["url"], published_at=extract_published_date(page.get('text', '')), collected_at=date.today().isoformat(),
                            text=text, allowed_departments=[], kind="external_excerpt")
            self.collected[source.id] = source
            self.notices.append("外部正文未人工入库审核；来源关联不代表医院等级已确认，发布日期需核对。")
            if unverified:
                self.notices.append("本轮包含身份未独立核验的公开网页；可以引用其实际记载，"
                                    "不能冒充已独立核验官网或行政认证，内容与时效须分别判断。")
            if page["reader"] == "crawl4ai" and not page.get("browser_diagnostics", {}).get("domcontentloaded", True):
                self.notices.append("浏览器页面尚未完成DOM加载，仅引用本次已读取的正文，不保证页面完整。")
            return {"ok": True, "evidence_ready": True, "sources": [source.model_dump()],
                    "authority": authority, "citation_scope": "attributed_public_statement" if unverified else "source_text",
                    "requires_attribution": unverified, "reader": page["reader"], "cached": cached,
                    "document_type": page["document_type"],
                    "reader_attempts": page.get("reader_attempts", []),
                    "browser_diagnostics": page.get("browser_diagnostics", {}),
                    "content_sha256": hashlib.sha256(page["text"].encode()).hexdigest(),
                    "hint": "检查医院/医生身份、相关性和覆盖度。quote须在同一原文片段内；"
                            "回答已有支持的部分，不因名单不完整或来源未独立核验而全部拒答。"
                            "自述等级、网页科室/医生介绍可按原文归属报告；行政认定、完整名单、"
                            "实时排班和医生成效不得超出正文证据。"}
        except (AccessError, ProviderFailure) as error:
            return failure(error)
        except asyncio.TimeoutError:
            return failure(AccessError("document_timeout", retryable=True))

    @budget_aware_reader
    @checkpoint_tool('map_website')
    async def map(self, raw):
        args = self.args(MapArgs, raw)
        if args is None:
            return self.invalid()
        try:
            page, cached = await self.load(args.candidate_id, args.query, args.method)
            links = list(page["links"])
            terms = {args.query.strip(), "医院", "简介", "科室", "住院", "医技", "业务", "门诊", "儿科", "内科", "外科", "评审", "三甲", ".pdf", "名单"} - {""}
            links.sort(key=lambda link: -(3*bool(args.query.strip() and args.query.strip() in (link["title"]+link["url"]))
                                         +sum(term in (link["title"] + link["url"]) for term in terms)))
            result = self.bounded([self.put(link, parent=args.candidate_id) for link in links[:args.limit]])
            return {"ok": True, "evidence_ready": False, "candidates": result, "cached": cached,
                    "reader": page["reader"], "reader_attempts": page.get("reader_attempts", []),
                    "browser_diagnostics": page.get("browser_diagnostics", {}),
                    "hint": "只发现这一页的实际链接。政府页面的医院链接可用verify_source核验；正文另行read。"}
        except (AccessError, ProviderFailure) as error:
            return failure(error)
        except asyncio.TimeoutError:
            return failure(AccessError("document_timeout", retryable=True))
