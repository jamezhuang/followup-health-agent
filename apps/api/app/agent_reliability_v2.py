"""V2：完整回答预复核、请求内复核复用、检索进度和诊断字段。"""
import asyncio
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
import os
import re
from urllib.parse import urlsplit

from . import agent_reliability as v1
from .schemas import Answer, Verification

_audit_cache = ContextVar("carebrief_answer_audit", default=None)
_progress = ContextVar("carebrief_retrieval_progress", default=None)

GENERATION_RULES = """
【V2：正文与候选分开】
summary、claims、department都是最终回答，具体医院事实必须有本轮已读取正文。
summary可自然整理结果，但其中具体医院名称、等级、官网自述、创办年份须由claims对应支持。
搜索摘要、标题和待读取链接只能用于发现候选，不能加入“已核实/有正文依据”的名单。
需要展示候选时另设“待核实线索”段落，不混入正文支持的名单，不在结尾把两者统称有正文依据。
第三方记载可以回答；按实际来源归属，不改称医院官网或行政认定。
不要求为了回答部分结果而搜齐完整名单；有足够相关正文时直接回答。
no_new_evidence或重复失败提示意味着应改用实质不同来源，或结束检索回答已有支持部分。
"""

VERIFICATION_RULES = """
【V2：完整输出逐项复核】
先逐句检查summary，再检查claims和department；summary不享有豁免。
具体医院的官网自述、等级、创办年份等须由本轮sources正文支持，并通过claims追溯到对应来源。
把第三方页面称为官网、把搜索摘要混入正文支持的名单，即使quote匹配也应supported=false。
搜索线索若明确单列为待核实、未据此断言事实，可以通过。
第三方原文可以支持“该页面记载”，不因非官网而一概拒答。
通过时说明检查过什么；失败时reason写出错误句与缺失依据，便于一次修正。
"""

EVENT_FIELDS = {
    "stage", "status", "duration_ms", "error_code", "tool", "source_count", "mode",
    "tool_call_id", "step", "used_calls", "repairs", "tool_call_count", "supported",
    "reason", "method", "exception_type", "field", "previous_value", "new_value",
    "basis", "persisted", "changed_fields", "age_before", "age_after", "cited_source_ids",
    "candidate_count", "outcome", "http_status", "cached", "reader", "authority",
    "document_type", "provider_attempt_count", "stop_reason", "no_progress_count",
    "new_source_count", "new_candidate_count", "max_calls", "max_steps", "retryable",
}


def enabled():
    return v1.enabled() and os.getenv("AGENT_RELIABILITY_V2_ENABLED", "true").lower() not in {"0", "false", "no"}


def preserve_event_details(event):
    """只保存诊断字段；不把任意参数、页面正文或密钥写入审计表。"""
    result = {}
    for key, value in event.items():
        if key not in EVENT_FIELDS:
            continue
        if isinstance(value, str):
            value = value[:2000 if key == "reason" else 400]
        elif isinstance(value, list):
            value = [str(item)[:200] for item in value[:30]]
        elif value is not None and not isinstance(value, (bool, int, float)):
            continue
        result[key] = value
    return result


def audited_run(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        token = _audit_cache.set({})
        try:
            return await function(*args, **kwargs)
        finally:
            _audit_cache.reset(token)
    return wrapped


def _payload(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _third_party(source):
    host = (urlsplit(source.get("url", "")).hostname or "").lower()
    return ("未独立核验" in source.get("institution", "")
            or any(host == h or host.endswith("."+h) for h in (
                "bendibao.com", "guahao.com", "yixue.com", "hotelaah.com", "familydoctor.com.cn", "120.net"))
            or any(w in source.get("title", "")+source.get("institution", "")
                   for w in ("本地宝", "医学百科", "挂号网")))


def _official_assertion(sentence):
    # “建议查官网”“官网读取失败”是说明/建议，不能当作伪造官网事实。
    return (bool(re.search(r"(?:官网|官方网站|官方介绍).{0,16}(?:自述|记载|介绍|显示|明确|指出|称|为|成立|创建)", sentence))
            and not re.search(r"(?:未能|未|没有|无法|不能|不是|并非|不代表|不等于|建议|请|可访问|可查询|需查阅|失败|超时|404)", sentence))


def check_source_attribution(answer, sources):
    if not enabled():
        return v1.check_source_attribution(answer, sources)
    old_error = v1.check_source_attribution(answer, sources)
    if old_error:
        return old_error
    data = _payload(answer)
    rows = {s["id"]: s for s in v1.source_rows(sources)}
    claims = data.get("claims", [])
    for claim in claims:
        source = rows.get(claim.get("evidence_id"), {})
        if _third_party(source) and _official_assertion(claim.get("text", "")):
            return "source_attribution_mismatch"
        host = urlsplit(source.get("url", "")).hostname or ""
        if host.endswith(".gov.cn") and re.search(r"(?:医院|卫生院).{0,4}官网", claim.get("text", "")) and _official_assertion(claim.get("text", "")):
            return "source_attribution_mismatch"
    cited = [(c, rows.get(c.get("evidence_id"), {})) for c in claims]
    if data.get("answer_basis") not in {"retrieved", "mixed"}:
        return None
    for sentence in re.split(r"[\n。；;]", data.get("summary", "")):
        if _official_assertion(sentence):
            supported = False
            for claim, source in cited:
                host = urlsplit(source.get("url", "")).hostname or ""
                if _third_party(source):
                    continue
                # 官网归属必须出现在对应claim；不能拿另一家医院官网给摘要背书。
                if not _official_assertion(claim.get("text", "")):
                    continue
                prefix = re.split(r"官网|官方网站|官方介绍", sentence, maxsplit=1)[0]
                names = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,35}?(?:医院|卫生院)", prefix)
                if names and host.endswith(".gov.cn"):
                    continue
                if names and not any(n in claim["text"] and n in source.get("text", "") for n in names):
                    continue
                phrases = re.findall(r"[“\"]([^”\"]{4,160})[”\"]", sentence)
                if phrases and not all(p in source.get("text", "") for p in phrases):
                    continue
                supported = True
            if not supported:
                return "summary_source_attribution_mismatch"
        years = re.findall(r"(?:创建|成立|始建|建院).{0,8}?((?:18|19|20)\d{2})年", sentence)
        years += re.findall(r"((?:18|19|20)\d{2})年.{0,8}?(?:创建|成立|始建|建院)", sentence)
        if years and not all(any(y in c.get("text", "") and y in s.get("text", "") for c, s in cited) for y in years):
            return "summary_unsupported_fact"
    return None


def repair_instructions(code):
    if code.startswith("semantic_answer_invalid") or code in {
        "summary_source_attribution_mismatch", "summary_unsupported_fact", "candidate_as_evidence",
    }:
        return code + "。修改完整回答：删除或准确归属不受正文支持的summary事实；候选线索单列为待核实；有依据的医院事实写入claims。不要为了修正措辞继续大规模检索。"
    return v1.repair_instructions(code)


async def log_evidence_failure(agent, state, answer, code):
    text = answer.summary if code.startswith("summary_") else "；".join(c.text for c in answer.claims)
    await agent.events.write(state["run_id"], "evidence_check", status="failed", error_code=code,
                             method="evidence_rule", supported=False,
                             reason=code+"；待修正文本："+text[:800],
                             cited_source_ids=[c.evidence_id for c in answer.claims])


def _audit_key(state, answer, sources):
    data = {"message": state.get("message"), "facts": state.get("facts"), "history": state.get("history"),
            "memory_context": state.get("memory_context"), "confirmed_reports": state.get("confirmed_reports"),
            "risk_inputs": state.get("risk_inputs"), "answer": _payload(answer), "sources": v1.source_rows(sources)}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def verify_with_diagnostics(agent, state, answer, used_sources):
    if not enabled():
        return await v1.verify_with_diagnostics(agent, state, answer, used_sources)
    key = _audit_key(state, answer, used_sources)
    cache = _audit_cache.get()
    if cache is not None and key in cache:
        await agent.events.write(state["run_id"], "verification_reused", status="ok",
                                 method="request_cache", supported=cache[key].supported,
                                 reason=cache[key].reason, duration_ms=0)
        return cache[key]
    error = check_source_attribution(answer, used_sources)
    if error:
        result = Verification(supported=False, reason=error)
        await agent.events.write(state["run_id"], "verification", status="failed", method="evidence_rule",
                                 supported=False, reason=error, error_code=error, duration_ms=0)
    else:
        result = await v1.verify_with_diagnostics(agent, state, answer, used_sources)
    if cache is not None:
        cache[key] = result
    return result


async def preflight_answer(agent, state, answer, sources):
    """网页回答在工具循环结束前复核，失败可修正；最终validate复用同一结果。"""
    if not enabled() or answer.answer_basis not in {"retrieved", "mixed"}:
        return None
    cited = {c.evidence_id for c in answer.claims}
    used = [s for s in v1.source_rows(sources) if s["id"] in cited]
    verification = await verify_with_diagnostics(agent, state, answer, used)
    if verification.supported:
        return None
    return "semantic_answer_invalid：" + verification.reason[:1200]


def _positive_int(name, default, minimum=1, maximum=100):
    try:
        number = int(os.getenv(name, str(default)))
    except ValueError:
        number = default
    return min(maximum, max(minimum, number))


class RetrievalProgress:
    def __init__(self):
        self.candidates, self.sources = set(), set()
        self.no_progress = 0
        self.limit = _positive_int("AGENT_RETRIEVAL_NO_PROGRESS_LIMIT", 4)
        self.failed_reads = {}
        self.last = {}

    def observe(self, name, result):
        if name not in {"search_web", "search_knowledge", "search_official_web", "read_webpage", "map_website"}:
            return
        candidates = {c.get("url") or c.get("candidate_id") for c in result.get("candidates", []) if isinstance(c, dict)} - {None}
        # 相同URL/正文以稳定指纹判断新增；避免变更source.id假装取得新证据。
        sources = {hashlib.sha256((s.get("url", "")+"\n"+s.get("text", "")).encode()).hexdigest()
                   for s in result.get("sources", []) if isinstance(s, dict) and s.get("text")}
        new_c, new_s = len(candidates-self.candidates), len(sources-self.sources)
        self.candidates.update(candidates)
        self.sources.update(sources)
        self.no_progress = 0 if new_c or new_s else self.no_progress+1
        self.last = {"new_candidate_count": new_c, "new_source_count": new_s, "no_progress_count": self.no_progress}

    def should_stop(self):
        return self.no_progress >= self.limit


def retrieval_should_stop():
    progress = _progress.get()
    return bool(progress and progress.should_stop())


def final_repair_allowed(stopping, repairs, remaining):
    # 原有非网页测试合同保持不变。网页预算停止后仍允许一次只修输出的机会。
    return repairs < 1 and remaining > 0 and (not stopping or _progress.get() is not None)


def efficient_tool_loop(function):
    signature = inspect.signature(function)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        handlers = bound.arguments["handlers"]
        active = enabled() and "search_web" in handlers
        if not active:
            return await function(*args, **kwargs)
        progress = RetrievalProgress()
        token = _progress.set(progress)
        callback = bound.arguments.get("on_event")
        async def emit(stage, **details):
            if callback:
                await callback(stage, **details)
        async def observed(stage, **details):
            if stage == "agent_tool" and details.get("status") == "started":
                progress.last = {}
            elif stage == "agent_tool" and details.get("status") in {"ok", "failed"} and not progress.last:
                # 原循环的缓存复用或wait_for超时没有进入handler尾部，也算一次无新增结果。
                progress.observe(details.get("tool"), {})
                await emit("retrieval_progress", status="ok", tool=details.get("tool"), **progress.last)
            if stage == "agent_budget" and progress.should_stop():
                details["stop_reason"] = "no_new_evidence"
                details["no_progress_count"] = progress.no_progress
            await emit(stage, **details)
        def wrapper(name, handler):
            async def invoke(raw):
                key = None
                if name in {"read_webpage", "map_website"} and isinstance(raw, dict):
                    key = (name, raw.get("candidate_id"), raw.get("method", "auto"))
                if key and key in progress.failed_reads:
                    result = {**progress.failed_reads[key], "cached": True,
                              "hint": "同一页面同一读取方式已有确定性失败；改query不能修复它。可换读取方式、换来源或回答已有依据。"}
                else:
                    result = await handler(raw)
                    if key and isinstance(result, dict) and not result.get("ok") and not result.get("retryable") and result.get("error") not in {
                        "invalid_arguments", "source_unverified", "government_page_not_read", "unknown_candidate", "tool_internal",
                    }:
                        progress.failed_reads[key] = dict(result)
                if isinstance(result, dict):
                    progress.observe(name, result)
                    await emit("retrieval_progress", status="ok", tool=name, **progress.last)
                    if progress.should_stop():
                        result = {**result, "stop_recommended": True,
                                  "hint": str(result.get("hint", ""))+" 连续调用没有新增候选或正文，结束检索并回答已有支持部分；不足部分明确说明。"}
                return result
            return invoke
        bound.arguments["handlers"] = {name: wrapper(name, handler) for name, handler in handlers.items()}
        bound.arguments["on_event"] = observed
        bound.arguments["max_calls"] = min(bound.arguments["max_calls"], _positive_int("AGENT_RETRIEVAL_MAX_CALLS", 10))
        bound.arguments["max_steps"] = min(bound.arguments["max_steps"], _positive_int("AGENT_RETRIEVAL_MAX_STEPS", 7))
        try:
            await emit("retrieval_limits", status="ok", max_calls=bound.arguments["max_calls"], max_steps=bound.arguments["max_steps"])
            return await function(*bound.args, **bound.kwargs)
        finally:
            _progress.reset(token)
    return wrapped
