"""确定性回归，不联网；通过不代表医学语义准确率。"""
import asyncio
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent_reliability_v2 import (
    RetrievalProgress, audited_run, check_source_attribution, preserve_event_details,
    preflight_answer, verify_with_diagnostics, repair_instructions, log_evidence_failure,
)
from app.schemas import Answer, Claim, Verification
from app.tool_loop import run_tool_loop, ToolLoopError
from scripts.apply_agent_reliability_v2 import patch_agent, patch_repository, patch_policy, patch_loop


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setenv("AGENT_RELIABILITY_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_V2_ENABLED", "true")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_ENABLED", "false")
    monkeypatch.setenv("AGENT_RETRIEVAL_MAX_CALLS", "10")
    monkeypatch.setenv("AGENT_RETRIEVAL_MAX_STEPS", "7")
    monkeypatch.setenv("AGENT_RETRIEVAL_NO_PROGRESS_LIMIT", "4")


def run(value):
    return asyncio.run(value)


def source(identity="s", text="佛山市中医院：三级甲等。", url="https://yyk.familydoctor.com.cn/test", institution="未独立核验"):
    return {"id": identity, "text": text, "url": url, "institution": institution,
            "title": "医院介绍", "kind": "external_excerpt", "published_at": None,
            "collected_at": "2026-10-09", "allowed_departments": []}


def answer(summary="该页面记载佛山市中医院为三级甲等。"):
    return Answer(summary=summary, answer_basis="retrieved", claims=[
        Claim(text="该页面记载佛山市中医院为三级甲等。", evidence_id="s", quote="佛山市中医院：三级甲等。")])


def test_summary_misattribution_rejected_even_when_claim_is_correct():
    value = answer('佛山市中医院官网页面自述为“三级甲等现代化大型中医医院”。')
    assert check_source_attribution(value, [source()]) == "summary_source_attribution_mismatch"


def test_summary_founding_year_without_evidence_rejected():
    assert check_source_attribution(answer("佛山市中医院创建于1956年。"), [source()]) == "summary_unsupported_fact"


@pytest.mark.parametrize("summary", ["该页面记载佛山市中医院为三级甲等。", "医院官网读取失败，本轮采用第三方记载。", "建议查阅医院官网确认最新信息。", "该页面不是医院官网，其记载仅供进一步核实。"])
def test_third_party_and_failed_read_explanations_are_not_blanket_rejected(summary):
    assert check_source_attribution(answer(summary), [source()]) is None


def test_official_claim_and_summary_with_actual_body_are_allowed():
    text = "佛山市中医院官网介绍：三级甲等现代化大型中医医院。"
    value = Answer(summary='佛山市中医院官网介绍为“三级甲等现代化大型中医医院”。', answer_basis="retrieved",
                   claims=[Claim(text=text, evidence_id="s", quote=text)])
    assert check_source_attribution(value, [source(text=text, url="https://hospital.example.org/info", institution="佛山市中医院")]) is None


def test_one_other_hospital_official_source_cannot_justify_summary():
    text = "甲医院官网介绍：三级甲等现代化大型中医医院。"
    value = Answer(summary='佛山市中医院官网介绍为“三级甲等现代化大型中医医院”。', answer_basis="retrieved",
                   claims=[Claim(text=text, evidence_id="s", quote=text)])
    assert check_source_attribution(value, [source(text=text, url="https://hospital.example.org/info", institution="甲医院")]) == "summary_source_attribution_mismatch"


def test_government_official_attribution_is_allowed():
    text = "卫生健康委官网记载医院评审结果。"
    value = Answer(summary=text, answer_basis="retrieved", claims=[Claim(text=text, evidence_id="s", quote=text)])
    assert check_source_attribution(value, [source(text=text, url="https://wsjkw.gd.gov.cn/result", institution="广东省卫生健康委")]) is None


def test_government_page_cannot_be_called_hospital_website():
    text = "佛山市中医院官网介绍为三级甲等。"
    value = Answer(summary=text, answer_basis="retrieved", claims=[Claim(text=text, evidence_id="s", quote=text)])
    assert check_source_attribution(value, [source(text=text, url="https://wsjkw.gd.gov.cn/result", institution="广东省卫生健康委")]) == "source_attribution_mismatch"


def test_event_filter_keeps_diagnostics_and_drops_secrets():
    event = preserve_event_details({"reason": "错误句不受正文支持", "supported": False, "method": "llm",
        "previous_value": "28岁", "new_value": "38岁", "changed_fields": ["age"], "candidate_count": 5,
        "password": "DO-NOT-STORE", "api_key": "DO-NOT-STORE", "arguments": {"query": "private"}})
    assert event["new_value"] == "38岁" and event["candidate_count"] == 5
    assert event["supported"] is False and event["reason"]
    assert not {"password", "api_key", "arguments"} & event.keys()


def test_event_details_are_bounded():
    result = preserve_event_details({"reason": "x"*5000, "changed_fields": ["age"]*100})
    assert len(result["reason"]) == 2000 and len(result["changed_fields"]) == 30


class Events:
    def __init__(self): self.rows = []
    async def write(self, run_id, stage, **details): self.rows.append({"stage": stage, **details})


class Verifier:
    def __init__(self, verdicts): self.verdicts = list(verdicts); self.calls = 0
    async def structured(self, schema, prompt, data):
        self.calls += 1
        return self.verdicts.pop(0)


def test_hard_evidence_rejection_records_candidate_text():
    agent = SimpleNamespace(events=Events())
    bad = answer("佛山市中医院官网页面自述为三甲。")
    run(log_evidence_failure(agent, state(), bad, "summary_source_attribution_mismatch"))
    event = agent.events.rows[-1]
    assert event["supported"] is False and event["method"] == "evidence_rule"
    assert "佛山市中医院官网" in event["reason"]


def state(identity="r"):
    return {"run_id": identity, "message": "佛山市有哪些三甲医院？", "facts": {}, "history": []}


def test_preflight_rejects_semantic_candidate_leak_and_records_reason():
    agent = SimpleNamespace(events=Events(), client=Verifier([Verification(supported=False, reason="摘要把未读取搜索候选列入有正文依据的名单")]))
    error = run(preflight_answer(agent, state(), answer("待核实搜索摘要也属于有正文依据名单。"), [source()]))
    assert error.startswith("semantic_answer_invalid") and "搜索候选" in error
    assert agent.events.rows[-1]["supported"] is False
    assert "只修" not in repair_instructions(error)  # 返回真正的完整修正说明。
    assert "待核实" in repair_instructions(error)


def test_same_answer_preflight_and_final_use_one_verifier_call():
    agent = SimpleNamespace(events=Events(), client=Verifier([Verification(supported=True, reason="完整回答与对应正文一致")]))
    @audited_run
    async def scenario():
        assert await preflight_answer(agent, state(), answer(), [source()]) is None
        assert (await verify_with_diagnostics(agent, state(), answer(), [source()])).supported
    run(scenario())
    assert agent.client.calls == 1
    assert any(e["stage"] == "verification_reused" for e in agent.events.rows)


def test_changed_answer_is_rechecked_and_failed_answer_is_not_reused():
    agent = SimpleNamespace(events=Events(), client=Verifier([
        Verification(supported=False, reason="summary与正文不一致"), Verification(supported=True, reason="修正后符合")]))
    @audited_run
    async def scenario():
        assert await preflight_answer(agent, state(), answer("错误的概述"), [source()])
        fixed = answer()
        assert await preflight_answer(agent, state(), fixed, [source()]) is None
        assert (await verify_with_diagnostics(agent, state(), fixed, [source()])).supported
    run(scenario())
    assert agent.client.calls == 2


def test_web_loop_semantic_repair_then_final_reuses_corrected_verdict():
    events = Events()
    agent = SimpleNamespace(events=events, client=Verifier([
        Verification(supported=False, reason="摘要把搜索候选混入正文名单"),
        Verification(supported=True, reason="修正后仅正文支持部分")]))
    collected = {}
    bad, fixed = answer("正文名单也包括未读取的搜索候选。"), answer()
    llm = LLM([call("1", "read_webpage", {"candidate_id": "c"}),
               {"content": bad.model_dump_json()}, {"content": fixed.model_dump_json()}])
    async def read(args): collected["s"] = source(); return {"ok": True, "sources": [source()]}
    async def emit(stage, **details): await events.write("r", stage, **details)
    async def validate(content): return await preflight_answer(agent, state(), Answer.model_validate_json(content), collected)
    @audited_run
    async def scenario():
        output = await run_tool_loop(llm, [], [], {"search_web": read, "read_webpage": read},
                                     on_event=emit, validate_final=validate)
        value = Answer.model_validate_json(output)
        assert value == fixed
        assert (await verify_with_diagnostics(agent, state(), value, [source()])).supported
    run(scenario())
    assert agent.client.calls == 2
    assert any(e.get("stage") == "agent_final" and e.get("status") == "failed" for e in events.rows)
    assert any(e.get("stage") == "verification_reused" for e in events.rows)


def test_concurrent_runs_do_not_share_audit_cache():
    agent = SimpleNamespace(events=Events(), client=Verifier([Verification(supported=True, reason="通过")]*2))
    @audited_run
    async def scenario():
        await verify_with_diagnostics(agent, state(), answer(), [source()])
        await asyncio.sleep(0)
        await verify_with_diagnostics(agent, state(), answer(), [source()])
    async def both(): await asyncio.gather(scenario(), scenario())
    run(both())
    assert agent.client.calls == 2


class LLM:
    def __init__(self, outputs): self.outputs = list(outputs); self.options = []; self.messages = []
    async def complete(self, messages, **options):
        self.options.append(options); self.messages.append(list(messages))
        return self.outputs.pop(0)


def call(identity, name, args):
    return {"content": None, "tool_calls": [{"id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}]}


def final(text="done"):
    return {"content": json.dumps({"summary": text, "answer_basis": "general", "claims": []})}


def execute(llm, handlers, events, validator=None, **kwargs):
    async def emit(stage, **details): events.append({"stage": stage, **details})
    return run(run_tool_loop(llm, [{"role": "user", "content": "合成检索任务"}], [], handlers,
        max_steps=kwargs.get("max_steps", 10), max_calls=kwargs.get("max_calls", 20),
        total_timeout=5, final_reserve=.2, on_event=emit, validate_final=validator))


def test_same_failed_page_reworded_query_does_not_refetch():
    invoked = []; events = []
    async def read(args): invoked.append(args); return {"ok": False, "error": "page_http_error", "http_status": 404, "retryable": False}
    async def search(args): return {"ok": True, "candidates": []}
    llm = LLM([call("1", "read_webpage", {"candidate_id": "c", "query": "简介", "method": "direct"}),
               call("2", "read_webpage", {"candidate_id": "c", "query": "三甲", "method": "direct"}), final()])
    execute(llm, {"search_web": search, "read_webpage": read}, events)
    assert len(invoked) == 1
    assert any(e.get("cached") for e in events if e["stage"] == "agent_tool")


def test_failed_direct_read_still_allows_browser_fallback():
    invoked = []; events = []
    async def read(args):
        invoked.append(args)
        return {"ok": False, "error": "page_http_error", "retryable": False} if args["method"] == "direct" else {"ok": True, "sources": [source()]}
    llm = LLM([call("1", "read_webpage", {"candidate_id": "c", "method": "direct"}),
               call("2", "read_webpage", {"candidate_id": "c", "method": "browser"}), final()])
    execute(llm, {"search_web": read, "read_webpage": read}, events)
    assert len(invoked) == 2


def test_state_dependent_denial_can_be_retried_after_verification():
    invoked = []; events = []
    async def read(args):
        invoked.append(args)
        return {"ok": False, "error": "source_unverified", "retryable": False} if len(invoked) == 1 else {"ok": True, "sources": [source()]}
    llm = LLM([call("1", "read_webpage", {"candidate_id": "c", "query": "a"}),
               call("2", "read_webpage", {"candidate_id": "c", "query": "b"}), final()])
    execute(llm, {"search_web": read, "read_webpage": read}, events)
    assert len(invoked) == 2


def test_no_progress_stops_with_final_tool_choice_none(monkeypatch):
    monkeypatch.setenv("AGENT_RETRIEVAL_NO_PROGRESS_LIMIT", "2")
    events = []; invoked = []
    async def search(args): invoked.append(args); return {"ok": True, "candidates": []}
    llm = LLM([call("1", "search_web", {"query": "a"}), call("2", "search_web", {"query": "b"}), final()])
    execute(llm, {"search_web": search}, events)
    assert llm.options[-1]["tool_choice"] == "none" and len(invoked) == 2
    assert any(e.get("stop_reason") == "no_new_evidence" for e in events)


def test_original_loop_cache_also_counts_no_progress(monkeypatch):
    monkeypatch.setenv("AGENT_RETRIEVAL_NO_PROGRESS_LIMIT", "2")
    events = []; invoked = []
    async def search(args): invoked.append(args); return {"ok": True, "candidates": []}
    llm = LLM([call("1", "search_web", {"query": "a"}), call("2", "search_web", {"query": "a"}), final()])
    execute(llm, {"search_web": search}, events)
    assert len(invoked) == 1 and llm.options[-1]["tool_choice"] == "none"


def test_new_body_resets_progress_and_same_body_new_id_does_not():
    progress = RetrievalProgress()
    progress.observe("search_web", {"candidates": []})
    progress.observe("read_webpage", {"sources": [source()]})
    assert progress.no_progress == 0
    progress.observe("read_webpage", {"sources": [source(identity="another")]})
    assert progress.no_progress == 1


def test_final_repair_allowed_after_web_tool_budget(monkeypatch):
    monkeypatch.setenv("AGENT_RETRIEVAL_MAX_CALLS", "1")
    events = []
    async def search(args): return {"ok": True, "candidates": []}
    llm = LLM([call("1", "search_web", {"query": "x"}), final("bad"), final("fixed")])
    async def validate(content): return "semantic_answer_invalid：错误句" if json.loads(content)["summary"] == "bad" else None
    assert "fixed" in execute(llm, {"search_web": search}, events, validator=validate)
    assert llm.options[-1]["tool_choice"] == "none"
    assert sum(e["stage"] == "agent_final" and e["status"] == "failed" for e in events) == 1


def test_second_failed_final_stops_without_infinite_repair(monkeypatch):
    monkeypatch.setenv("AGENT_RETRIEVAL_MAX_CALLS", "1")
    async def search(args): return {"ok": True, "candidates": []}
    async def validate(content): return "semantic_answer_invalid：错误句"
    llm = LLM([call("1", "search_web", {"query": "x"}), final("bad"), final("bad")])
    with pytest.raises(ToolLoopError, match="Final answer failed validation"):
        execute(llm, {"search_web": search}, [], validator=validate)
    assert len(llm.options) == 3


def test_configured_call_cap_requests_final(monkeypatch):
    monkeypatch.setenv("AGENT_RETRIEVAL_MAX_CALLS", "2")
    async def search(args): return {"ok": True, "candidates": [{"url": "https://example.org/"+args["query"]}]}
    llm = LLM([call("1", "search_web", {"query": "a"}), call("2", "search_web", {"query": "b"}), final()])
    events = []; execute(llm, {"search_web": search}, events)
    assert llm.options[-1]["tool_choice"] == "none"
    assert next(e for e in events if e["stage"] == "retrieval_limits")["max_calls"] == 2


def test_v2_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_RELIABILITY_V2_ENABLED", "false")
    assert check_source_attribution(answer("其官网页面自述为三甲。"), [source()]) is None


def test_patch_repository_preserves_fields_without_database():
    original = 'class Repository:\n    def record_event(self, run_id, event):\n        allowed = {"stage"}\n        return {k:v for k,v in event.items() if k in allowed}\n'
    patched = patch_repository(original)
    tree = ast.parse(patched)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "record_event")
    from app.agent_reliability_v2 import EVENT_FIELDS
    namespace = {"EVENT_FIELDS": EVENT_FIELDS, "preserve_event_details": preserve_event_details}
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<record-event>", "exec"), namespace)
    result = namespace["record_event"](None, "r", {"stage": "verification", "reason": "通过原因", "method": "llm", "supported": True, "password": "secret"})
    assert result["reason"] == "通过原因" and result["method"] == "llm" and result["supported"] is True
    assert "password" not in result and patch_repository(patched) == patched


def test_patch_agent_adds_preflight_and_preserves_v1_age_decorators():
    text = '''# CAREBRIEF_RELIABILITY_V1
from .agent_reliability import reliable_classify, reliable_retrieve, check_source_attribution, verify_with_diagnostics
class Agent:
    async def run(self): pass
    @reliable_classify
    async def classify(self): pass
    @reliable_retrieve
    async def retrieve(self):
        async def check_final(content):
            if content is None: return "invalid"
            return None
'''
    patched = patch_agent(text)
    assert "@reliable_classify" in patched and "@reliable_retrieve" in patched and "@audited_run" in patched
    assert "return await preflight_answer" in patched and patch_agent(patched) == patched


def test_installed_patch_is_idempotent():
    root = Path(__file__).resolve().parents[1]
    for path, patch in [("app/agent.py", patch_agent), ("app/answer_policy.py", patch_policy), ("app/tool_loop.py", patch_loop)]:
        text = (root/path).read_text(encoding="utf-8-sig")
        assert patch(patch(text)) == patch(text)


def test_structure_mismatch_stops_before_edit():
    with pytest.raises(ValueError): patch_agent("class Agent: pass")
    with pytest.raises(ValueError): patch_repository("class Repository: pass")
