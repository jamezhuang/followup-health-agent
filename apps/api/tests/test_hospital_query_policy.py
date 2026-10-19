import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.hospital_query_policy import hospital_list_evidence_required
from scripts.fix_hospital_query_policy import patch
from scripts.evaluate_context_compaction import BEFORE, analyze, safe_trace_events


@pytest.mark.parametrize("message", [
    BEFORE[0],
    "不需查询医院名单，只讨论就诊准备。",
    "不用查医院名单，我只整理纸质报告。",
    "不需要医院名单，请说明就诊准备。",
    "不要列出佛山三甲医院，先说明带什么材料。",
    "不是让你查询医院名单，是让你解释报告整理。",
    "我计划周三去公立医院，医院尚未选定。",
    "三甲医院是什么意思？",
    "解释三级甲等医院的概念和标准。",
    "为什么查询医院名单要附来源？",
    "医院名单为什么检索不到？",
    "普宁市人民医院有哪些科室？",
    "广州中医院有哪些医生？",
    "普宁华侨医院眼科有没有知名医生？",
    "儿童就诊前有哪些注意事项？",
    "医院医生有哪些建议？",
    "",
    None,
])
def test_preparation_negation_and_department_are_not_hospital_list(message):
    assert hospital_list_evidence_required(message) is False


@pytest.mark.parametrize("message", [
    "请查询佛山市有哪些三甲医院",
    "广州有哪些医院？",
    "佛山有哪些三级甲等医院？",
    "列出佛山医院名单。",
    "告诉我佛山市三甲医院名单。",
    "佛山市三甲医院",
    "哪些医院设有儿科？",
    "哪家医院有运动医学科？",
    "佛山有哪些医院设有内分泌科？",
    "不需查询广州医院名单，另外请列出佛山三甲医院。",
    "不要查广州医院名单而是列出佛山三甲医院。",
    "不需要完整名单，推荐两家佛山三甲医院。",
    "不用联网，告诉我佛山有哪些三甲医院。",
    "不要调用工具，列出佛山三甲医院。",
    "不需要查官网但请给我佛山三甲医院名单。",
    "请核实佛山有三甲医院吗？",
    "佛山有三甲医院吗？",
    "佛山三甲医院名单是什么？",
])
def test_real_hospital_query_still_requires_evidence(message):
    assert hospital_list_evidence_required(message) is True


SOURCE = '''"""agent module"""
from __future__ import annotations
class Agent:
    async def check_final(self, state, basis, search_attempted):
        list_question = "医院" in state["message"] and any(
            word in state["message"] for word in ("三甲", "三级甲等", "医院名单", "哪些医院", "有哪些")
        )
        if self.settings.external_enabled and list_question:
            if basis == "general":
                return "hospital_list_needs_sources"
            if basis == "insufficient" and not search_attempted:
                return "external_search_not_attempted"
        return await self.preflight(state)
    async def preflight(self, state):
        return state.get("quote_error")
'''


def patched_agent():
    text = patch(SOURCE)
    scope = {"__name__": "app._guard_fixture", "__package__": "app"}
    exec(compile(text, "guard_fixture.py", "exec"), scope)
    agent = scope["Agent"]()
    agent.settings = SimpleNamespace(external_enabled=True)
    return agent


def test_patch_keeps_future_import_and_is_idempotent():
    revised = patch(SOURCE)
    assert patch(revised) == revised
    assert revised.index("from __future__") < revised.index("from .hospital_query_policy")
    assert 'return "hospital_list_needs_sources"' in revised
    assert 'return "external_search_not_attempted"' in revised
    assert 'return await self.preflight(state)' in revised


def test_patched_guard_accepts_original_round_one_but_still_runs_preflight():
    agent = patched_agent()
    assert asyncio.run(agent.check_final({"message": BEFORE[0]}, "general", False)) is None
    assert asyncio.run(agent.check_final({"message": BEFORE[0], "quote_error": "invalid_citation"}, "general", False)) == "invalid_citation"


def test_patched_guard_keeps_actual_query_and_search_attempt_checks():
    agent = patched_agent()
    state = {"message": "请查询佛山市有哪些三甲医院"}
    assert asyncio.run(agent.check_final(state, "general", False)) == "hospital_list_needs_sources"
    assert asyncio.run(agent.check_final(state, "insufficient", False)) == "external_search_not_attempted"
    assert asyncio.run(agent.check_final(state, "insufficient", True)) is None
    assert asyncio.run(agent.check_final({**state, "quote_error": "invalid_citation"}, "retrieved", True)) == "invalid_citation"


def test_patch_rejects_missing_or_ambiguous_target():
    with pytest.raises(ValueError):
        patch('print("hello")\n')
    with pytest.raises(ValueError):
        patch(SOURCE + '\nlist_question = True\n')


def test_safe_events_keep_failure_order_and_drop_free_form_bodies():
    events = [
        {"stage": "agent_final", "status": "failed", "error_code": "hospital_list_needs_sources", "repairs": 0,
         "prompt": "secret-patient", "content": "secret-answer", "error": "secret-provider-body"},
        {"stage": "agent_budget", "status": "stopped", "stop_reason": "answer_repair_only", "used_calls": 0},
        {"stage": "agent_final", "status": "failed", "error_code": "hospital_list_needs_sources", "repairs": 1},
        {"stage": "agent_termination", "status": "failed", "error_code": "final_validation_failed"},
        {"stage": "failed", "status": "failed", "error_code": "agent_output_invalid"},
    ]
    safe = safe_trace_events(events)
    assert "secret" not in json.dumps(safe)
    assert safe[1]["stop_reason"] == "answer_repair_only"
    result = analyze([{"round": 1, "status": 503, "events": safe, "context": {"status": "recent_only"}}], False, {})
    assert [event["error_code"] for event in result["failure_events"]] == [
        "hospital_list_needs_sources", "hospital_list_needs_sources", "final_validation_failed", "agent_output_invalid"]
    assert result["failure_events"][0]["round"] == 1
    assert result["history_budget_pass"] is None


def test_safe_events_reject_bad_types_and_are_bounded():
    assert safe_trace_events(None) == []
    assert safe_trace_events([None, {"stage": "秘密病史"}]) == []
    assert safe_trace_events([{"stage": "tool", "used_calls": True, "step": -1}]) == [{"stage": "tool"}]
    assert len(safe_trace_events([{"stage": "tool"}] * 400)) == 300


def test_live_runner_saves_failure_events_without_reposting(tmp_path, monkeypatch):
    from scripts import evaluate_context_compaction as runner
    posts = []
    def handler(request):
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(200, json={})
        if path == "/auth/me":
            return httpx.Response(200, json={"user": {"id": "owner"}})
        if path == "/sessions":
            return httpx.Response(200, json={"id": "session"})
        if path == "/sessions/session":
            return httpx.Response(200, json={"messages": []})
        if path.endswith("/messages"):
            posts.append(path)
            return httpx.Response(503, json={"code": "agent_output_invalid", "run_id": "run"})
        if path == "/workspace/runs/run":
            return httpx.Response(200, json={"events": [
                {"stage": "context_compaction", "status": "recent_only"},
                {"stage": "agent_final", "status": "failed", "error_code": "hospital_list_needs_sources", "content": "secret-password"},
                {"stage": "failed", "status": "failed", "error_code": "agent_output_invalid"}]})
        raise AssertionError(path)
    original = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(runner.getpass, "getpass", lambda _: "secret-password")
    args = SimpleNamespace(output=tmp_path / "out", phase="before", email="test@example.invalid",
        base_url="http://127.0.0.1:18000", timeout=1, summary_db=tmp_path / "missing.db")
    with pytest.raises(ValueError):
        asyncio.run(runner.run(args))
    assert len(posts) == 1
    saved = (args.output / "checkpoint.json").read_text(encoding="utf-8")
    assert "secret-password" not in saved
    report = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
    assert report["failure_events"][0]["error_code"] == "hospital_list_needs_sources"
