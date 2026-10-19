"""有范围的事实更正、来源归属检查、复核诊断与浏览器预算降级。"""
import asyncio
from datetime import date
from functools import wraps
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .schemas import Answer, Classification, Observation, Verification


GENERATION_RULES = """
【可靠性补充规则】
明确更正以最新原文为准，不能因history含旧年龄而拒绝新年龄。
只更正信息时简短确认，不重新展开病因、检查清单或用药。
summary和claims中的具体医院/医生事实遵守同样的证据要求。
逐字quote存在还不够：对应source.text必须支持claim的全部实质事实。
本地宝、挂号平台、百科等第三方页面不能称为医院官网；政府文件也不等于医院官网。
来源身份未独立核验时使用‘该页面记载’，仍可回答有原文依据的部分。
一条quote只证明等级时，不附加原文没有的创办年份、实力、排名或预约情况。
历史评审有效期届满只能报告历史认定，不能直接推断现在失去或仍有三甲资格。
当前问题不要求完整名单时，有相关证据即可回答该部分；不要为凑数量耗尽预算。
浏览器预算耗尽后选择direct/已配置extract或已有正文，不重复请求browser。
基础科普简短回答；区分抗生素、解热镇痛药与非甾体抗炎药，不混称其作用。
"""

VERIFICATION_RULES = """
【可靠性复核补充】
latest_input中的明确本人更正覆盖历史旧值，facts是本轮合并后状态。
不能因为当前38岁与历史28岁不同就拒绝；检查是否正确确认更正，不凭空添病史。
检查summary和claims全部具体事实的来源归属；第三方页面被称为医院官网应拒绝。
不能仅凭一段‘三级甲等’支持创办年份、综合实力和最新等级状态。
部分结果允许通过，检索失败或来源未独立核验不是单独拒绝理由。
reason必须指出具体错误句及依据，或明确说明通过原因，不只写‘不支持’。
"""


def enabled():
    return os.getenv("AGENT_RELIABILITY_ENABLED", "true").lower() not in {"0", "false", "no"}


def explicit_age_correction(message):
    """只认完整的本人年龄更正句；不从症状、他人、假设、提问里猜年龄。"""
    if not isinstance(message, str) or len(message) > 100:
        return None
    pattern = (
        r"\s*(?:(?:更正(?:一下)?|纠正(?:一下)?|刚才(?:说|写)错了|我刚才(?:说|写)错了)[：:,，。\s]*)?"
        r"我(?:今年|现在)?(?:是)?(?P<age>[1-9]\d{0,2})岁"
        r"(?:[，,\s]*(?:不是|并非)\d{1,3}岁)?[。.!！\s]*"
    )
    match = re.fullmatch(pattern, message)
    if not match or not any(cue in message for cue in ("更正", "纠正", "说错", "写错", "不是", "并非")):
        return None
    age = int(match.group("age"))
    if not 1 <= age <= 120:
        return None
    return {"value": str(age)+"岁", "evidence": match.group("age")+"岁"}


def correction_answer(value):
    return Answer(summary=f"已更正：你的年龄为{value}。本次会话后续会以这个年龄为准。",
                  answer_basis="general")


def reliable_classify(function):
    @wraps(function)
    async def wrapped(self, state):
        update = explicit_age_correction(state.get("message"))
        if enabled() and self.settings.mode == "live" and update:
            previous = dict(state.get("facts") or {})
            facts = {**previous, "age": update["value"]}
            await self.events.write(state["run_id"], "classify", status="started", mode="live")
            await self.events.write(state["run_id"], "facts_update", status="ok", field="age",
                                    previous_value=previous.get("age", ""), new_value=update["value"],
                                    basis="explicit_latest_input", persisted=False)
            classification = Classification(intent="update", supported=True,
                observations=[Observation(name="age", value=update["value"], evidence=update["evidence"], operation="set")])
            return {"classification": classification, "facts": facts, "risk_inputs": []}
        result = await function(self, state)
        if enabled() and self.settings.mode == "live" and isinstance(result, dict):
            old, new = state.get("facts", {}), result.get("facts", {})
            changed = [key for key, value in new.items() if old.get(key) != value]
            await self.events.write(state["run_id"], "facts_merge", status="ok", changed_fields=changed,
                                    age_before=old.get("age", ""), age_after=new.get("age", ""), persisted=False)
        return result
    return wrapped


def reliable_retrieve(function):
    @wraps(function)
    async def wrapped(self, state):
        update = explicit_age_correction(state.get("message"))
        if (enabled() and self.settings.mode == "live" and update
                and state.get("facts", {}).get("age") == update["value"]):
            await self.events.write(state["run_id"], "answer", status="ok", mode="deterministic_fact_update")
            return {"answer": correction_answer(update["value"]), "sources": [], "retrieval_notice": ""}
        return await function(self, state)
    return wrapped


def source_rows(sources):
    values = sources.values() if isinstance(sources, dict) else (sources or [])
    return [s.model_dump() if hasattr(s, "model_dump") else s for s in values if isinstance(s, dict) or hasattr(s, "model_dump")]


def check_source_attribution(answer, sources):
    """高确定性检查，不冒充完整语义判定；其余交给内容复核与人工评审。"""
    if not enabled():
        return None
    payload = answer.model_dump() if hasattr(answer, "model_dump") else answer
    indexed = {s["id"]: s for s in source_rows(sources)}
    for claim in payload.get("claims", []):
        source = indexed.get(claim["evidence_id"])
        if source is None or claim["quote"] not in source.get("text", "") or "[原文片段边界]" in claim["quote"]:
            return "invalid_citation"
        text = claim["text"]
        institution = source.get("institution", "")
        title = source.get("title", "")
        host = (urlsplit(source.get("url", "")).hostname or "").lower()
        third_party = ("未独立核验" in institution or any(t in title+institution for t in ("本地宝", "医学百科", "挂号网"))
                       or host == "bendibao.com" or host.endswith(".bendibao.com")
                       or host == "guahao.com" or host.endswith(".guahao.com")
                       or host == "yixue.com" or host.endswith(".yixue.com"))
        official_assertion = False
        for part in re.split(r"[。；;\n，,]", text):
            match = re.search(r"官网|官方网站|官方介绍", part)
            if match and not re.search(r"(?:并非|不是|不能称为|不代表|无法核实|未能核实|没有).{0,12}$", part[:match.start()]):
                official_assertion = True
        if third_party and official_assertion:
            return "source_attribution_mismatch"
        year = re.search(r"(?:创建|成立|始建|建院).{0,8}?((?:18|19|20)\d{2})年", text)
        reverse_year = re.search(r"((?:18|19|20)\d{2})年.{0,8}?(?:创建|成立|始建|建院)", text)
        if (year or reverse_year) and (year or reverse_year).group(1) not in source.get("text", ""):
            return "unsupported_source_fact"
    return None


def repair_instructions(code):
    if code in {"source_attribution_mismatch", "unsupported_source_fact"}:
        return code+"。请同时修正summary与所有claims：按真实页面归属表述，不把第三方称为官网；删除对应原文没有的创办年份、排名、实力等附加事实，保留有依据的部分。"
    return code


async def log_evidence_failure(agent, state, answer, code):
    await agent.events.write(state["run_id"], "evidence_check", status="failed", error_code=code,
                             cited_source_ids=[c.evidence_id for c in answer.claims])


async def verify_with_diagnostics(agent, state, answer, used_sources):
    from .answer_policy import VERIFY_PROMPT
    update = explicit_age_correction(state.get("message"))
    rule_checked = (enabled() and update and state.get("facts", {}).get("age") == update["value"]
                    and answer.model_dump() == correction_answer(update["value"]).model_dump()
                    and not used_sources and not state.get("risk_inputs"))
    started = time.monotonic()
    await agent.events.write(state["run_id"], "verification", status="started",
                             method="validated_fact_update" if rule_checked else "llm")
    data = {"latest_input": state["message"], "history": state.get("history", []),
            "facts": state.get("facts", {}), "memory_context": state.get("memory_context", {}),
            "confirmed_reports": state.get("confirmed_reports", []), "answer": answer.model_dump(),
            "sources": source_rows(used_sources)}
    try:
        verification = Verification(supported=True, reason="完整本人年龄更正句、合并后事实和规则生成确认一致。") if rule_checked else await agent.client.structured(Verification, VERIFY_PROMPT, data)
    except Exception as error:
        await agent.events.write(state["run_id"], "verification", status="failed",
                                 error_code=getattr(error, "code", "verification_failed"), exception_type=type(error).__name__)
        raise
    await agent.events.write(state["run_id"], "verification", status="ok" if verification.supported else "failed",
                             supported=verification.supported, reason=verification.reason,
                             error_code=None if verification.supported else "unsupported_answer",
                             method="validated_fact_update" if rule_checked else "llm",
                             duration_ms=round((time.monotonic()-started)*1000),
                             cited_source_ids=[c.evidence_id for c in answer.claims])
    if os.getenv("AGENT_DIAGNOSTICS_ENABLED", "false").lower() in {"1", "true", "yes"}:
        # 默认不存快照。开发环境开关开启后，文件按run_id关联；包含本轮文本/事实，勿放真实病例。
        root = Path(os.getenv("AGENT_DIAGNOSTICS_DIR", ".cache/agent_diagnostics"))
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(state["run_id"]))[:100]
        snapshot = {"run_id": state["run_id"], "latest_input": state["message"], "facts": state.get("facts", {}),
                    "history": state.get("history", []),
                    "answer": answer.model_dump(), "sources": source_rows(used_sources),
                    "verification": verification.model_dump(), "method": "rule" if rule_checked else "llm"}
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root/(safe_id+".json")).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            await agent.events.write(state["run_id"], "diagnostic_snapshot", status="failed",
                                     exception_type=type(error).__name__)
    return verification


def extract_published_date(text):
    """只使用文章时间标签或前30行独立日期；不把‘今天是’当发布日期。"""
    if not isinstance(text, str):
        return None
    candidates = re.findall(r"(?:发布时间|发布日期|发表时间|时间)\s*[：:]\s*((?:19|20)\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text[:40000])
    if not candidates:
        for line in text.splitlines()[:30]:
            match = re.fullmatch(r"\s*((?:19|20)\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*", line)
            if match:
                candidates.append(match.groups())
                break
    for year, month, day in candidates:
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
    return None


def budget_aware_reader(function):
    @wraps(function)
    async def wrapped(self, raw):
        args = dict(raw) if isinstance(raw, dict) else raw
        exhausted = enabled() and getattr(self, "_reliability_browser_exhausted", False)
        if exhausted and isinstance(args, dict):
            method = args.get("method", "auto")
            cached_page = self.pages.get(args.get("candidate_id"))
            if method == "browser" and not (cached_page and cached_page.get("reader") == "crawl4ai"):
                return {"ok": False, "error": "browser_budget_exceeded", "retryable": False,
                        "available_read_methods": ["direct"],
                        "hint": "本轮浏览器预算耗尽。改用direct或已有正文；不要再请求browser。已支持部分可以直接回答。"}
            if method == "auto" and args.get("candidate_id") not in self.pages:
                args["method"] = "direct"
        result = await function(self, args)
        if enabled() and result.get("error") == "browser_budget_exceeded":
            self._reliability_browser_exhausted = True
            result = {**result, "available_read_methods": ["direct"],
                      "hint": "本轮浏览器预算耗尽；停止browser尝试，改用direct/已配置extract或已有相关正文回答，不必补齐完整名单。"}
        elif exhausted:
            result = {**result, "browser_budget_exhausted": True,
                      "hint": str(result.get("hint", ""))+" 本轮browser不可用；若direct也失败请换页或回答已有支持的部分。"}
        return result
    return wrapped


def observed_tool_loop(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        callback = kwargs.get("on_event")
        starts = {}
        async def emit(stage, **details):
            key = (stage, details.get("tool_call_id", details.get("step")))
            if stage in {"agent_model", "agent_tool"}:
                if details.get("status") == "started":
                    starts[key] = time.monotonic()
                elif key in starts:
                    details.setdefault("duration_ms", round((time.monotonic()-starts.pop(key))*1000))
            if callback:
                await callback(stage, **details)
        kwargs["on_event"] = emit
        try:
            return await function(*args, **kwargs)
        except asyncio.CancelledError:
            await emit("agent_termination", status="failed", error_code="request_cancelled")
            raise
        except Exception as error:
            message = str(error) if type(error).__name__ == "ToolLoopError" else ""
            code = {"Agent context exceeds limit": "history_budget_exceeded",
                    "Malformed tool call": "invalid_tool_call", "Invalid tool batch": "invalid_tool_batch",
                    "Agent time budget exceeded": "agent_time_budget_exceeded",
                    "Model time budget exceeded": "model_timeout",
                    "Final answer failed validation": "final_validation_failed",
                    "Invalid model response": "invalid_model_response"}.get(message, getattr(error, "code", "tool_loop_failed"))
            await emit("agent_termination", status="failed", error_code=code, exception_type=type(error).__name__)
            raise
    return wrapped
