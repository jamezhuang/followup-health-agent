"""V3：通用回答可修正，原文片段选择、逐条引用诊断与复核契约。"""
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import time
from difflib import SequenceMatcher

from . import agent_reliability as v1
from . import agent_reliability_v2 as v2
from .schemas import Answer, Verification

_scope = ContextVar("carebrief_v3_output", default=None)
BOUNDARY = "[原文片段边界]"

GENERATION_RULES = """
【V3：选择原文片段与输出修正】
工具结果中的evidence_units是后端从该sources原文切出的连续片段。
有适用片段时，claim.evidence_id仍填写对应source_id，quote可填写unit_id（例如EV-…）。
后端会在校验和保存前将unit_id替换为其完整连续原文，客户端看到真实quote。
选择的片段必须支持claim，编号只解决原文复制问题，不证明语义正确。
也兼容quote直接复制连续原文；不能改写、补字、跨片段边界拼接。
通用知识可以使用general、claims=[]，并给出有条件的一般科室方向department；
这不是指定医院科室，也不能据此确诊。信息不足时应必要追问。
收到复核失败后，只用已有事实/原文修正一次完整Answer，不继续消耗检索工具。
"""

VERIFICATION_RULES = """
【V3：后端字段契约】
结构合法性由后端完成，structural_validation是后端的检查结果，不是用户要求。
general允许claims为空、department填写有条件的一般就诊方向；mixed也允许该方向。
只有insufficient必须department为空；retrieved的department需有引用正文支持。
不得因为general有department，或者一般科室方向没有来源，就判为不支持。
仍检查科室是否结合输入、是否无条件确诊、是否忽略危险信号或冒充指定医院信息。
只复核实质内容、上下文、风险及引用支持。reason指出具体错误句和证据缺口，
不能自行增加schema约束，不能把一般方向等同于医院实际挂号目录。
"""

EVENT_FIELDS = v2.EVENT_FIELDS | {
    "claim_index", "evidence_id", "quote", "citation_reason", "source_found", "quote_found",
    "evidence_unit_ids", "evidence_unit_count", "resolved_quote_count", "answer_basis",
}


def enabled():
    return v2.enabled() and os.getenv("AGENT_RELIABILITY_V3_ENABLED", "true").lower() not in {"0", "false", "no"}


def preserve_event_details(event):
    result = v2.preserve_event_details(event)
    for key in EVENT_FIELDS-v2.EVENT_FIELDS:
        value = event.get(key)
        if key not in event:
            continue
        if isinstance(value, str): result[key] = value[:600 if key == "quote" else 200]
        elif isinstance(value, list): result[key] = [str(item)[:100] for item in value[:12]]
        elif value is None or isinstance(value, (bool, int, float)): result[key] = value
    return result


def audited_run(function):
    original = v2.audited_run(function)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        token = _scope.set({"repair_pending": False, "citation_issues": []})
        try:
            return await original(*args, **kwargs)
        finally:
            _scope.reset(token)
    return wrapped


def build_units(sources):
    """每段最多500字符，保留空白和原文，不跨片段边界。"""
    units = []
    for source in v1.source_rows(sources):
        text = source.get("text", "")
        if not isinstance(text, str) or not source.get("id"):
            continue
        for part in text.split(BOUNDARY):
            for start in range(0, len(part), 500):
                fragment = part[start:start+500]
                if not fragment.strip(): continue
                digest = hashlib.sha256((source["id"]+"\0"+fragment).encode()).hexdigest()[:20]
                units.append({"unit_id": "EV-"+digest, "source_id": source["id"], "text": fragment})
    return units


def recommended_units(sources, query="", limit=6):
    units = build_units(sources)
    words = re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", query)
    words += ["三级甲等", "拟确定", "评审", "科室", "诊疗范围", "有效期"]
    units.sort(key=lambda u: -sum(word in u["text"] for word in words))
    return units[:limit]


def attach_units(result, query=""):
    if not enabled() or not isinstance(result, dict) or not result.get("ok") or not result.get("sources"):
        return result
    output = dict(result)
    candidates = recommended_units(result["sources"], query)
    output["evidence_units"] = []
    for unit in candidates:
        proposed = {**output, "evidence_units": output["evidence_units"]+[unit]}
        try:
            size = len(json.dumps(proposed, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError):
            return result
        if size > 19500: break  # 原工具循环的20000字符结果上限仍有效。
        output["evidence_units"].append(unit)
    return output


def inject_initial_evidence(content, sources):
    if not enabled(): return content
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return content
    if not isinstance(payload, dict): return content
    payload["evidence_units"] = recommended_units(sources)
    return json.dumps(payload, ensure_ascii=False)


def resolve_evidence_quotes(answer, sources):
    if not enabled(): return answer
    index = {u["unit_id"]: u for u in build_units(sources)}
    resolved = []
    for claim in answer.claims:
        token = claim.quote.strip().strip("[]")
        unit = index.get(token)
        # 必须属于本轮真实sources以及同一evidence_id；其他情况保持原值供校验拒绝。
        if unit and unit["source_id"] == claim.evidence_id:
            claim = claim.model_copy(update={"quote": unit["text"]})
        resolved.append(claim)
    basis = answer.answer_basis or ("retrieved" if resolved else "general")
    return answer.model_copy(update={"claims": resolved, "answer_basis": basis})


def citation_diagnostics(answer, sources):
    rows = {s["id"]: s for s in v1.source_rows(sources)}
    units = build_units(sources)
    unit_index = {u["unit_id"]:u for u in units}
    problems = []
    for index, claim in enumerate(answer.claims, 1):
        source = rows.get(claim.evidence_id)
        token = claim.quote.strip().strip("[]")
        unit = unit_index.get(token)
        reason = None
        if source is None: reason = "unknown_source_id"
        elif unit and unit["source_id"] != claim.evidence_id: reason = "evidence_unit_source_mismatch"
        elif re.fullmatch(r"EV-[0-9a-f]{20}", token) and not unit: reason = "unknown_or_stale_evidence_unit"
        elif BOUNDARY in claim.quote: reason = "cross_excerpt_boundary"
        elif claim.quote not in source.get("text", ""): reason = "quote_not_in_source"
        if reason:
            related = [u for u in units if u["source_id"] == claim.evidence_id]
            related.sort(key=lambda u: -SequenceMatcher(None, claim.quote, u["text"]).ratio())
            problems.append({"claim_index": index, "evidence_id": claim.evidence_id,
                             "citation_reason": reason, "quote": claim.quote,
                             "source_found": source is not None, "quote_found": False,
                             "evidence_unit_ids": [u["unit_id"] for u in related[:3]],
                             "suggested_units": related[:2]})
    return problems


async def _snapshot(agent, state, answer, sources, phase, verdict=None):
    if os.getenv("AGENT_DIAGNOSTICS_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(state["run_id"]))[:100]
    root = Path(os.getenv("AGENT_DIAGNOSTICS_DIR", ".cache/agent_diagnostics"))
    payload = {"run_id": state["run_id"], "phase": phase, "latest_input": state.get("message"),
               "history": state.get("history", []), "facts": state.get("facts", {}),
               "answer": answer.model_dump(), "sources": v1.source_rows(sources),
               "verification": verdict.model_dump() if verdict else None}
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root/f"{safe_id}.v3.{phase}.{time.time_ns()}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except OSError as error:
        await agent.events.write(state["run_id"], "diagnostic_snapshot", status="failed", exception_type=type(error).__name__)


async def log_evidence_failure(agent, state, answer, code, sources=None):
    if not enabled() or code != "invalid_citation":
        return await v2.log_evidence_failure(agent, state, answer, code)
    problems = citation_diagnostics(answer, sources if sources is not None else state.get("sources", []))
    scope = _scope.get()
    if scope is not None:
        scope["citation_issues"] = problems
        scope["repair_pending"] = True
    for problem in problems:
        details = {k:v for k,v in problem.items() if k != "suggested_units"}
        await agent.events.write(state["run_id"], "citation_validation", status="failed",
                                 error_code="invalid_citation", method="exact_source_text", **details)
    await agent.events.write(state["run_id"], "evidence_check", status="failed", error_code=code,
                             supported=False, reason="引用不匹配，详见citation_validation中的声明序号和quote。",
                             cited_source_ids=[c.evidence_id for c in answer.claims])
    await _snapshot(agent, state, answer, sources if sources is not None else state.get("sources", []), "citation_failure")


def repair_instructions(code):
    scope = _scope.get()
    if enabled() and scope is not None:
        scope["repair_pending"] = True
        if code == "invalid_citation" and scope["citation_issues"]:
            feedback = [{"claim_index": p["claim_index"], "evidence_id": p["evidence_id"],
                         "reason": p["citation_reason"], "bad_quote": p["quote"][:200],
                         "available_units": p["suggested_units"]} for p in scope["citation_issues"][:3]]
            return code+"。只修正答案，不再检索。以下是后端诊断和可选连续原文；quote填写适用unit_id，evidence_id保持该unit的source_id。若片段不支持claim，应删除对应claim并同步收缩summary。"+json.dumps(feedback, ensure_ascii=False)
        if code.startswith("semantic_answer_invalid"):
            return code+"。只修正一次完整回答。general的claims=[]且department为有条件一般方向是合法结构；修正真正的内容问题，不必强制补引用。"
    return v2.repair_instructions(code)


def final_repair_only():
    return bool(enabled() and _scope.get() and _scope.get()["repair_pending"])


def structural_contract(answer):
    basis = answer.answer_basis or ("retrieved" if answer.claims else "general")
    issues = []
    if basis in {"general", "insufficient"} and answer.claims: issues.append("invalid_answer_basis")
    if basis in {"retrieved", "mixed"} and not answer.claims: issues.append("missing_citation")
    if basis == "insufficient" and answer.department: issues.append("unsupported_department")
    return {"passed": not issues, "issues": issues, "answer_basis": basis,
            "general_department_allowed": True, "mixed_department_allowed": True,
            "insufficient_department_must_be_empty": True, "retrieved_department_requires_evidence": True}


async def verify_with_diagnostics(agent, state, answer, used_sources):
    if not enabled(): return await v2.verify_with_diagnostics(agent, state, answer, used_sources)
    update = v1.explicit_age_correction(state.get("message"))
    if (update and state.get("facts", {}).get("age") == update["value"]
            and answer.model_dump() == v1.correction_answer(update["value"]).model_dump()
            and not used_sources and not state.get("risk_inputs")):
        return await v2.verify_with_diagnostics(agent, state, answer, used_sources)
    key = "v3:"+v2._audit_key(state, answer, used_sources)
    cache = v2._audit_cache.get()
    if cache is not None and key in cache:
        verdict = cache[key]
        await agent.events.write(state["run_id"], "verification_reused", status="ok" if verdict.supported else "failed",
                                 supported=verdict.supported, reason=verdict.reason, method="request_cache", duration_ms=0)
        return verdict
    contract = structural_contract(answer)
    rule_error = contract["issues"][0] if contract["issues"] else v2.check_source_attribution(answer, used_sources)
    await agent.events.write(state["run_id"], "validation_contract", status="failed" if rule_error else "ok",
                             answer_basis=contract["answer_basis"], error_code=rule_error, method="backend_rules")
    started = time.monotonic()
    if rule_error:
        verdict = Verification(supported=False, reason=rule_error)
        method = "backend_rules"
    else:
        from .answer_policy import VERIFY_PROMPT
        method = "llm_content_review"
        await agent.events.write(state["run_id"], "verification", status="started", method=method)
        data = {"latest_input": state["message"], "history": state.get("history", []), "facts": state.get("facts", {}),
                "memory_context": state.get("memory_context", {}), "confirmed_reports": state.get("confirmed_reports", []),
                "answer": answer.model_dump(), "sources": v1.source_rows(used_sources), "structural_validation": contract}
        try:
            verdict = await agent.client.structured(Verification, VERIFY_PROMPT, data)
        except Exception as error:
            await agent.events.write(state["run_id"], "verification", status="failed", method=method,
                                     error_code=getattr(error, "code", "verification_failed"), exception_type=type(error).__name__)
            raise
    await agent.events.write(state["run_id"], "verification", status="ok" if verdict.supported else "failed",
                             supported=verdict.supported, reason=verdict.reason, method=method,
                             error_code=None if verdict.supported else "unsupported_answer",
                             duration_ms=round((time.monotonic()-started)*1000), cited_source_ids=[c.evidence_id for c in answer.claims])
    await _snapshot(agent, state, answer, used_sources, "verification", verdict)
    # 只复用通过结果。一次修正可能得到同一份合法答案，复核器上次误判不能永久锁死它。
    if cache is not None and verdict.supported: cache[key] = verdict
    return verdict


async def preflight_answer(agent, state, answer, sources):
    if not enabled(): return await v2.preflight_answer(agent, state, answer, sources)
    contract = structural_contract(answer)
    if contract["issues"]: return contract["issues"][0]
    cited = {c.evidence_id for c in answer.claims}
    used = [s for s in v1.source_rows(sources) if s["id"] in cited]
    verdict = await verify_with_diagnostics(agent, state, answer, used)
    return None if verdict.supported else "semantic_answer_invalid："+verdict.reason


def evidence_tool_loop(function):
    signature = inspect.signature(function)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        if not enabled(): return await function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        callback = bound.arguments.get("on_event")
        async def emit(stage, **details):
            if stage == "agent_budget" and final_repair_only(): details["stop_reason"] = "answer_repair_only"
            if callback: await callback(stage, **details)
        def decorate(handler):
            async def invoke(raw):
                return attach_units(await handler(raw), raw.get("query", "") if isinstance(raw, dict) else "")
            return invoke
        bound.arguments["handlers"] = {name: decorate(handler) for name, handler in bound.arguments["handlers"].items()}
        bound.arguments["on_event"] = emit
        return await function(*bound.args, **bound.kwargs)
    return wrapped
