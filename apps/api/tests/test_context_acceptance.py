import ast
import json
import sqlite3
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.context_compaction_metrics import context_compaction_metrics
from scripts.install_context_acceptance import patch_repository, patch_integration
from scripts.evaluate_context_compaction import analyze, db_snapshot, history_estimate, BEFORE, AFTER


def test_context_metrics_allow_only_typed_content_free_values():
    event = {"stage": "context_compaction", "cursor_after": 12, "persistent": True,
             "summary_calls": True, "after_estimate": "secret", "cursor_before": -1,
             "summary": "private health text", "owner": "private owner",
             "estimator": "utf8_byte_upper_bound", "binding": "arbitrary-private-text"}
    assert context_compaction_metrics(event) == {
        "cursor_after": 12, "persistent": True, "estimator": "utf8_byte_upper_bound"}
    assert context_compaction_metrics({**event, "stage": "tool"}) == {}
    assert context_compaction_metrics({"stage": "context_compaction", "error": "secret-error-body"}) == {"compaction_error": "other_summary_error"}
    assert context_compaction_metrics({"stage": "context_compaction", "error": "TimeoutError"}) == {"compaction_error": "TimeoutError"}


def test_repository_patch_preserves_other_fields_and_is_idempotent():
    source = '''"""仓库说明"""
from __future__ import annotations
class Repository:
    def record_event(self, run_id, event):
        allowed = {"stage", "status"}
        return RunEvent(run_id=run_id, payload={"time": now(), **{k:v for k,v in event.items() if k in allowed}})
    def other(self):
        return "保持"
'''
    revised = patch_repository(source)
    assert patch_repository(revised) == revised
    assert 'return "保持"' in revised and 'allowed = {"stage", "status"}' in revised
    tree = ast.parse(revised)
    assert isinstance(tree.body[1], ast.ImportFrom) and tree.body[1].module == "__future__"
    captured = {}
    def event(**data):
        captured.update(data)
    scope = {"RunEvent": event, "now": lambda: "time"}
    exec(compile(revised, "patched", "exec"), scope)
    scope["Repository"]().record_event("run", {"stage": "context_compaction", "status": "compressed",
        "cursor_after": 10, "summary": "private", "duration_ms": "unallowed"})
    assert captured["payload"] == {"time": "time", "stage": "context_compaction", "status": "compressed", "cursor_after": 10}


def test_integration_patch_does_not_change_signature_or_calls():
    source = '''async def prepare_context(agent, history, facts, run_id, current_message=None):
    cfg = Config.from_env()
    details = {}
    details["duration_ms"] = 1
    await agent.events.write(run_id, "context_compaction", **details)
    return history
'''
    revised = patch_integration(source)
    assert patch_integration(revised) == revised
    assert "current_message=None" in revised and "target_budget=cfg.target" in revised
    assert "return history" in revised


def test_missing_or_stripped_metrics_cannot_count_as_budget_pass():
    rows = [{"round": 1, "status": 200, "context": {"status": "compressed"}}]
    result = analyze(rows, False, {})
    assert result["history_budget_pass"] is None
    assert not result["summary_trigger_verified"]
    assert result["restart_summary_recovery"] is None
    assert result["actual_provider_tokens"] is None and not result["billing_savings_verified"]


def test_history_estimate_matches_core_units_and_rejects_tool_scratch():
    rows = [{"role": "user", "content": "你好"}]
    assert history_estimate(rows) == len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()) + 16
    assert history_estimate([{"role": "tool", "content": "正文"}]) is None


def test_restart_evidence_requires_same_summary_and_correct_cursor():
    summary = {"available": True, "exists": True, "version": 3, "cursor": 18, "summary_sha256": "abc"}
    restart = {"saved_before_restart": summary, "read_before_first_after_message": dict(summary)}
    row = {"round": 22, "phase": "after", "status": 200, "context": {
        "persistent": True, "cursor_before": 18, "reset": False}}
    assert analyze([row], False, restart)["restart_summary_recovery"] is True
    row["context"]["cursor_before"] = 0
    assert analyze([row], False, restart)["restart_summary_recovery"] is False


def test_summary_db_probe_is_owner_scoped_and_does_not_export_summary(tmp_path):
    path = tmp_path / "summary.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE context_summaries(owner TEXT, session TEXT, version INTEGER, cursor INTEGER, summary TEXT)")
        db.execute("INSERT INTO context_summaries VALUES ('alice','s',2,8,'private-summary')")
    assert not db_snapshot(path, "bob", "s")["exists"]
    row = db_snapshot(path, "alice", "s")
    assert row["cursor"] == 8 and "private-summary" not in json.dumps(row)


def test_budget_checks_and_reduction_keep_estimates_separate_from_billing():
    row = {"round": 10, "status": 200, "full_history_estimate": 10000, "context": {
        "status": "compressed", "after_estimate": 3000, "summary_estimate": 500,
        "target_budget": 4200, "trigger_budget": 6000, "summary_calls": 1}}
    result = analyze([row], False, {})
    assert result["history_budget_pass"] is True
    assert result["history_size_comparisons"][0]["estimated_reduction_ratio"] == .7
    row["context"]["after_estimate"] = 4500
    assert analyze([row], False, {})["history_budget_pass"] is False


def test_acceptance_has_24_turns_and_keeps_restart_probes_separate():
    assert len(BEFORE) == 21 and len(AFTER) == 3
    assert "佛山" in BEFORE[8] and "周五上午" in BEFORE[14]
    assert "后端已经重启" in AFTER[0]


@pytest.mark.parametrize("failure", ["post_timeout", "login_denied", "trace_timeout"])
def test_runner_does_not_retry_posts_or_turn_trace_failure_into_chat_failure(tmp_path, monkeypatch, failure):
    from scripts import evaluate_context_compaction as runner
    sent = []
    messages = []
    def handler(request):
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(401 if failure == "login_denied" else 200, json={})
        if path == "/auth/me":
            return httpx.Response(200, json={"user": {"id": "owner"}})
        if path == "/sessions":
            return httpx.Response(200, json={"id": "session"})
        if path == "/sessions/session" and request.method == "GET":
            return httpx.Response(200, json={"messages": messages})
        if path.endswith("/messages"):
            sent.append(request)
            if failure == "post_timeout":
                raise httpx.ReadTimeout("secret-provider-body", request=request)
            question = json.loads(request.content)["message"]
            messages.extend([{"role": "user", "content": question},
                             {"role": "assistant", "content": "answer-secret-password"}])
            return httpx.Response(200, json={"messages": messages, "last_turn": {"run_id": "run" + str(len(sent))}})
        if path.startswith("/workspace/runs/"):
            raise httpx.ReadTimeout("trace-body-secret", request=request)
        raise AssertionError(path)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs:
        original_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(runner.getpass, "getpass", lambda _: "secret-password")
    args = SimpleNamespace(output=tmp_path / "output", phase="before", email="test@example.invalid",
        base_url="http://127.0.0.1:18000", timeout=1, summary_db=tmp_path / "missing.db")
    if failure == "trace_timeout":
        asyncio.run(runner.run(args))
        assert len(sent) == 21
    else:
        with pytest.raises(ValueError):
            asyncio.run(runner.run(args))
        assert len(sent) == (1 if failure == "post_timeout" else 0)
    saved = (args.output / "checkpoint.json").read_text(encoding="utf-8")
    assert "secret-password" not in saved and "secret-provider-body" not in saved and "trace-body-secret" not in saved
    state = json.loads(saved)
    assert state["before_complete"] is (failure == "trace_timeout")
    assert runner.analyze(state["rows"], False, {})["history_budget_pass"] is None
