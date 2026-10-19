import asyncio
import ast
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.classification_recovery import classify_with_repair, extraction_issue
from app.providers import ProviderError
from app.schemas import Classification, Observation, RiskMention
from scripts.fix_classification_recovery import patch


class Client:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def structured(self, schema, prompt, data):
        self.calls.append((schema, prompt, deepcopy(data)))
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return output


class Events:
    def __init__(self):
        self.rows = []

    async def write(self, run_id, stage, **details):
        self.rows.append({"stage": stage, **details})


def state(message="针对刚才的城市更正，哪些准备清单可以继续用？"):
    return {"message": message, "facts": {"age": "28"}, "history": [
        {"role": "user", "content": "我28岁，有糖尿病。"},
        {"role": "assistant", "content": "好的。"}], "memory_context": {}, "run_id": "run"}


def merge(message, facts, observations):
    """An injected strict merger; production uses the existing merge_observations."""
    for item in observations:
        if item.evidence not in message or item.value not in item.evidence:
            raise ValueError("source mismatch")
        value = item.value if item.operation == "set" else facts.get(item.name, "") + item.value
        if len(value) > 400:
            raise ValueError("capacity")
        facts[item.name] = value
    return facts


def candidate(observations=None, risks=None, intent="knowledge"):
    return Classification(intent=intent, supported=True, observations=observations or [], risks=risks or [])


def observation(value="糖尿病", evidence="我28岁，有糖尿病。", name="history", operation="set"):
    return Observation(name=name, value=value, evidence=evidence, operation=operation)


def run(outputs, input_state=None, merger=merge):
    agent = SimpleNamespace(client=Client(outputs), events=Events())
    current = input_state or state()
    before = deepcopy(current)
    answer = asyncio.run(classify_with_repair(agent, current, "分类原规则", merger))
    assert current == before
    return answer, agent


def test_valid_preparation_needs_no_extra_model_call():
    result, agent = run([candidate()])
    assert result.observations == []
    assert len(agent.client.calls) == 1
    assert agent.events.rows == [{"stage": "classify_validation", "status": "ok", "step": 0}]


def test_history_quote_is_repaired_once_before_fact_write():
    bad = candidate([observation()])
    result, agent = run([bad, candidate()])
    assert result.observations == []
    assert len(agent.client.calls) == 2
    assert agent.client.calls[1][2]["validation_error"] == "observation_evidence_mismatch"
    assert agent.events.rows[-1]["status"] == "ok"
    assert agent.events.rows[-1]["step"] == 1


@pytest.mark.parametrize("bad", [
    candidate([observation()]),
    candidate([observation(value="糖尿病", evidence="准备清单")]),
    candidate(risks=[RiskMention(symptom="咳血", evidence="咳血", status="present")]),
])
def test_twice_invalid_never_writes_candidate_facts_or_fake_risks(bad):
    result, agent = run([bad, bad])
    assert result.observations == [] and result.risks == []
    assert len(agent.client.calls) == 2
    assert agent.events.rows[-1]["error_code"] == "extraction_degraded_not_applied"
    assert agent.events.rows[-1]["status"] == "degraded"


def test_valid_new_age_remains_available_for_original_merge():
    current = state("更正，我29岁。")
    good = candidate([observation(value="29", evidence="我29岁", name="age")], intent="update")
    result, _ = run([good], current)
    assert current["facts"]["age"] == "28"
    updated = merge(current["message"], dict(current["facts"]), result.observations)
    assert updated["age"] == "29"


def test_repair_can_correct_evidence_and_preserve_valid_patient_fact():
    current = state("我29岁。")
    bad = candidate([observation(value="29", evidence="年龄是29岁", name="age")])
    good = candidate([observation(value="29", evidence="我29岁", name="age")])
    result, agent = run([bad, good], current)
    assert result.observations[0].value == "29"
    assert len(agent.client.calls) == 2


def test_append_overflow_does_not_truncate_or_overwrite_old_fact():
    current = state("我头痛。")
    current["facts"]["history"] = "旧" * 400
    bad = candidate([observation(value="头痛", evidence="我头痛", operation="append")])
    result, agent = run([bad, bad], current)
    assert result.observations == [] and current["facts"]["history"] == "旧" * 400
    assert agent.events.rows[0]["error_code"] == "extraction_merge_rejected"


def test_valid_risk_survives_repair_of_unrelated_bad_fact():
    current = state("我现在喘不过气。")
    risk = RiskMention(symptom="呼吸困难", evidence="喘不过气", status="present")
    result, _ = run([candidate([observation()], [risk]), candidate()], current)
    assert result.observations == []
    assert result.risks == [risk]


def test_valid_risk_survives_degradation_and_keeps_status():
    current = state("我现在喘不过气。")
    risk = RiskMention(symptom="呼吸困难", evidence="喘不过气", status="uncertain")
    bad = candidate([observation()], [risk])
    result, _ = run([bad, bad], current)
    assert result.risks == [risk]


def test_absent_risk_is_not_changed_to_present():
    current = state("没有咳血。")
    risk = RiskMention(symptom="咳血", evidence="没有咳血", status="absent")
    result, _ = run([candidate(risks=[risk])], current)
    assert result.risks[0].status == "absent"


@pytest.mark.parametrize("code", ["missing_key", "provider_http", "provider_timeout", "provider_format", "truncated"])
def test_network_authentication_and_other_provider_errors_are_not_swallowed(code):
    agent = SimpleNamespace(client=Client([ProviderError(code, "secret-body")]), events=Events())
    with pytest.raises(ProviderError) as caught:
        asyncio.run(classify_with_repair(agent, state(), "规则", merge))
    assert caught.value.code == code and len(agent.client.calls) == 1


def test_repair_provider_failure_still_propagates():
    agent = SimpleNamespace(client=Client([candidate([observation()]), ProviderError("provider_timeout", "secret-body")]), events=Events())
    with pytest.raises(ProviderError) as caught:
        asyncio.run(classify_with_repair(agent, state(), "规则", merge))
    assert caught.value.code == "provider_timeout" and len(agent.client.calls) == 2


def test_schema_failure_is_repaired_and_repeat_failure_can_degrade():
    result, agent = run([ProviderError("invalid_schema", "secret-body"), candidate()])
    assert result.observations == [] and len(agent.client.calls) == 2
    result, agent = run([ProviderError("invalid_schema", "secret-body"), ProviderError("invalid_schema", "secret-body")])
    assert result.intent == "other" and result.observations == []
    assert agent.events.rows[-1]["status"] == "degraded"


def test_corrupted_existing_facts_are_not_hidden_by_empty_fallback():
    def rejecting_merge(*args):
        raise ValueError("bad existing facts")
    agent = SimpleNamespace(client=Client([candidate(), candidate()]), events=Events())
    with pytest.raises(ProviderError) as caught:
        asyncio.run(classify_with_repair(agent, state(), "规则", rejecting_merge))
    assert caught.value.code == "invalid_extraction"
    assert agent.events.rows[-1]["error_code"] == "existing_facts_merge_rejected"


def test_unexpected_merger_bug_is_not_suppressed():
    def bug(*args):
        raise TypeError("implementation bug")
    with pytest.raises(TypeError):
        run([candidate()], merger=bug)


def test_run_events_do_not_include_patient_values_or_error_bodies():
    result, agent = run([candidate([observation()]), candidate([observation()])])
    labels = str(agent.events.rows)
    assert "糖尿病" not in labels and "28" not in labels and "secret" not in labels


SOURCE = '''"""agent fixture"""
from __future__ import annotations
class Agent:
    async def classify(self, state):
        early_risks = explicit_risks(state["message"])
        if early_risks:
            classification = classify_demo(state["message"], state["facts"])
        elif self.settings.mode == "demo":
            classification = classify_demo(state["message"], state["facts"])
        else:
            classification = await self.client.structured(
                Classification, CLASSIFY_PROMPT,
                {"latest_input": state["message"], "facts": state["facts"], "history": state["history"], "memory_context": {}})
        try:
            facts = merge_observations(state["message"], state["facts"], classification.observations)
        except ValueError:
            raise ProviderError("invalid_extraction", "not saved")
        risks = list(early_risks)
        for risk in classification.risks:
            if risk.evidence not in state["message"]:
                raise ProviderError("invalid_extraction", "bad risk")
            if risk.status in {"present", "uncertain"}:
                risks.append(risk.evidence)
        return {"facts": facts, "classification": classification, "risk_inputs": risks}
    async def retrieve(self, state):
        return "keep all earlier patches"
'''


def fixture_agent(outputs):
    scope = {"__name__": "app._classification_fixture", "__package__": "app", "Classification": Classification,
        "CLASSIFY_PROMPT": "原规则", "ProviderError": ProviderError, "merge_observations": merge,
        "explicit_risks": lambda message: ["喘不过气"] if "喘不过气" in message else [],
        "classify_demo": lambda message, facts: candidate()}
    exec(compile(patch(SOURCE), "fixture.py", "exec"), scope)
    agent = scope["Agent"]()
    agent.client = Client(outputs)
    agent.events = Events()
    agent.settings = SimpleNamespace(mode="live")
    return agent


def test_patched_classify_continues_round_ten_after_two_bad_extractions():
    agent = fixture_agent([candidate([observation()]), candidate([observation()])])
    result = asyncio.run(agent.classify(state()))
    assert result["facts"] == {"age": "28"}
    assert result["classification"].observations == []
    assert len(agent.client.calls) == 2


def test_patch_preserves_demo_and_explicit_risk_bypass_without_model_calls():
    agent = fixture_agent([])
    result = asyncio.run(agent.classify(state("我现在喘不过气。")))
    assert result["risk_inputs"] == ["喘不过气"] and agent.client.calls == []
    agent.settings.mode = "demo"
    asyncio.run(agent.classify(state()))
    assert agent.client.calls == []


def test_patch_preserves_all_other_statements_by_ast_comparison_and_is_idempotent():
    revised = patch(SOURCE)
    assert patch(revised) == revised
    before, after = ast.parse(SOURCE), ast.parse(revised)
    old_method = next(n for n in ast.walk(before) if isinstance(n, ast.AsyncFunctionDef) and n.name == "classify")
    new_method = next(n for n in ast.walk(after) if isinstance(n, ast.AsyncFunctionDef) and n.name == "classify")
    old = next(n for n in ast.walk(old_method) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Await))
    new = next(n for n in ast.walk(new_method) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Await))
    old.value = new.value
    after.body = [n for n in after.body if not (isinstance(n, ast.ImportFrom) and n.module == "classification_recovery")]
    assert ast.dump(before) == ast.dump(after)
    assert revised.index("from __future__") < revised.index("from .classification_recovery")


def test_patch_refuses_missing_target():
    with pytest.raises(ValueError):
        patch("class Agent: pass\n")
