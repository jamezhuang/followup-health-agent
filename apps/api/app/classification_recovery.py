"""Bounded recovery for classification output, preserving the real merge policy."""
from copy import deepcopy
from pydantic import ValidationError

from .providers import ProviderError
from .schemas import Classification

RECOVERABLE = {"invalid_schema", "invalid_extraction"}
RULES = """
本轮补充规则：observations只写最新输入明确给出的患者本人事实。
城市、星期、就诊准备计划、报告整理建议不是年龄、症状、持续时间或病史，
不要为了填字段把这些内容塞进history或associated。
history/facts/memory_context中的旧内容只能帮助理解，不能复制为最新输入的证据。
evidence必须是latest_input连续原文，value必须逐字包含在同一evidence内。
未明确给出新患者事实时，observations应为空；风险证据也只能来自最新输入。
不从缺少新信息推断用户没有风险，不将未核实的候选写成已确认档案。
"""


def extraction_issue(candidate, message, facts, merge):
    for item in candidate.observations:
        if not item.evidence.strip() or item.evidence not in message:
            return "observation_evidence_mismatch"
        if not item.value.strip() or item.value not in item.evidence:
            return "observation_value_mismatch"
    for item in candidate.risks:
        if not item.evidence.strip() or item.evidence not in message:
            return "risk_evidence_mismatch"
    try:
        # Preview only. The original Agent.classify still performs the real merge.
        merge(message, deepcopy(facts), deepcopy(candidate.observations))
    except ValueError:
        return "extraction_merge_rejected"
    return None


def verified_risks(candidates, message):
    """Keep source-backed risk mentions even if other fields need recovery."""
    found, seen = [], set()
    for candidate in candidates:
        for risk in candidate.risks:
            if not risk.evidence.strip() or risk.evidence not in message:
                continue
            key = (risk.symptom, risk.evidence, risk.status)
            if key not in seen:
                seen.add(key)
                found.append(risk)
    return found[:8]  # Classification schema has max_length=8.


async def record(agent, state, status, **fields):
    # No extracted values, quote text, prompts, secrets or raw exception bodies.
    await agent.events.write(state["run_id"], "classify_validation", status=status, **fields)


async def classify_with_repair(agent, state, instruction, merge):
    """At most two structured calls. Authentication/network errors still propagate."""
    payload = {
        "latest_input": state["message"],
        "facts": deepcopy(state["facts"]),
        "history": state["history"],
        "memory_context": state.get("memory_context", {}),
    }
    candidates = []
    issue = None
    for step in range(2):
        prompt = instruction + RULES
        data = dict(payload)
        if step:
            prompt += "\n上一轮输出未通过校验。只修正分类与提取，不回答用户问题。最多此次修正。"
            data["validation_error"] = issue
            await agent.events.write(state["run_id"], "classify_repair", status="started", step=1)
        try:
            candidate = await agent.client.structured(Classification, prompt, data)
            # Also validate test adapters and custom client implementations.
            if not isinstance(candidate, Classification):
                candidate = Classification.model_validate(candidate)
        except ProviderError as error:
            if error.code not in RECOVERABLE:
                raise
            issue = error.code
        except ValidationError:
            issue = "invalid_schema"
        else:
            candidates.append(candidate)
            issue = extraction_issue(candidate, state["message"], state["facts"], merge)
            if not issue:
                # A repair must not erase a previously valid risk mention merely
                # because an unrelated observation was rejected.
                candidate = candidate.model_copy(update={"risks": verified_risks(candidates, state["message"])})
                await record(agent, state, "ok", step=step)
                return candidate
        await record(agent, state, "failed", step=step, error_code=issue)

    # No unverified observations enter facts. Latest input/history still reach
    # the answer model through the original workflow; this is not an auto-answer.
    base = candidates[-1] if candidates else Classification(intent="other", supported=False)
    fallback = base.model_copy(update={
        "observations": [],
        "risks": verified_risks(candidates, state["message"]),
    })
    remaining_issue = extraction_issue(fallback, state["message"], state["facts"], merge)
    if remaining_issue:
        # Existing fact corruption/capacity must not be silently overwritten.
        await record(agent, state, "failed", error_code="existing_facts_merge_rejected")
        raise ProviderError("invalid_extraction", "已有会话事实未通过校验，本轮未更新，请检查运行记录。") from None
    await record(agent, state, "degraded", step=1, error_code="extraction_degraded_not_applied")
    return fallback
