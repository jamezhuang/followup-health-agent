"""Regress the real v3 filter -> repository -> acceptance-analysis path."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.context_compaction_metrics import context_compaction_metrics
from scripts.fix_context_metrics_order import MARKER, patch_repository
from scripts.evaluate_context_compaction import analyze


SOURCE = '''"""运行记录仓库，保留既有过滤规则。"""
class Repository:
    def record_event(self, run_id, event):
        allowed = EVENT_FIELDS
        event = preserve_event_details(event)
        with Session(self.engine) as db:
            if not db.get(Run, run_id):
                return
            db.add(RunEvent(run_id=run_id, payload={"time": now(), **{k:v for k,v in event.items() if k in allowed}, **context_compaction_metrics(event)}))
            db.commit()
    def other(self):
        return "保持原有逻辑"
'''


def installed_filters():
    # Only execute installed pure filter definitions, avoiding decorator/import
    # side effects and any database/model setup in the reliability modules.
    root = Path(__file__).resolve().parents[1] / "app"
    def load(name, v2=None):
        path = root / name
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        definitions = [node for node in tree.body if
            (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
             and target.id == "EVENT_FIELDS" for target in node.targets)) or
            (isinstance(node, ast.FunctionDef) and node.name == "preserve_event_details")]
        assert len(definitions) == 2
        scope = {"v2": v2}
        exec(compile(ast.fix_missing_locations(ast.Module(body=definitions, type_ignores=[])), str(path), "exec"), scope)
        return SimpleNamespace(EVENT_FIELDS=scope["EVENT_FIELDS"], preserve_event_details=scope["preserve_event_details"])
    return load("agent_reliability_v3.py", load("agent_reliability_v2.py"))


def persist(source, event, exists=True):
    filters = installed_filters()
    class DB:
        def __init__(self, engine):
            self.engine = engine
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, model, identity):
            return object() if exists else None
        def add(self, row):
            self.engine.rows.append(row.payload)
        def commit(self):
            self.engine.commits += 1
    engine = SimpleNamespace(rows=[], commits=0)
    scope = {"Session": DB, "Run": object(), "RunEvent": lambda **values: SimpleNamespace(**values),
             "now": lambda: "test-time", "EVENT_FIELDS": filters.EVENT_FIELDS,
             "preserve_event_details": filters.preserve_event_details,
             "context_compaction_metrics": context_compaction_metrics}
    exec(compile(source, "repository_fixture.py", "exec"), scope)
    repo = scope["Repository"]()
    repo.engine = engine
    repo.record_event("run", event)
    return engine


def event():
    return {"stage": "context_compaction", "status": "compressed", "duration_ms": 20,
            "after_estimate": 3000, "trigger_budget": 6000, "target_budget": 4200,
            "summary_budget": 1000, "summary_calls": 1, "summary_input_estimate": 5000,
            "summary_output_estimate": 700, "summary_estimate": 700,
            "cursor_before": 12, "cursor_after": 20, "persistent": True, "reset": False,
            "password": "secret", "api_key": "secret", "summary": "private report",
            "owner": "private owner", "arguments": {"query": "private query"}}


def test_original_order_reproduces_loss_with_actual_v3_filter():
    stored = persist(SOURCE, event()).rows[0]
    assert stored == {"time": "test-time", "stage": "context_compaction",
                      "status": "compressed", "duration_ms": 20}


@pytest.mark.parametrize("source", [SOURCE, ast.unparse(ast.parse(SOURCE)) + "\n"],
                         ids=["original-format", "reformatted"])
def test_fixed_order_retains_safe_metrics_and_filters_secrets(source):
    fixed = patch_repository(source)
    assert patch_repository(fixed) == fixed
    engine = persist(fixed, event())
    assert engine.commits == 1
    stored = engine.rows[0]
    for key, value in context_compaction_metrics(event()).items():
        assert stored[key] == value
    assert not {"password", "api_key", "summary", "owner", "arguments"} & stored.keys()
    assert stored["duration_ms"] == 20 and stored["status"] == "compressed"
    assert 'return "保持原有逻辑"' in fixed or "return '保持原有逻辑'" in fixed


def test_normal_tool_events_keep_existing_filter_without_compaction_fields():
    data = {**event(), "stage": "tool", "tool": "search_knowledge", "source_count": 3}
    original = persist(SOURCE, data).rows[0]
    fixed = persist(patch_repository(SOURCE), data).rows[0]
    assert fixed == original and "after_estimate" not in fixed


def test_invalid_metrics_are_not_promoted_into_audit():
    data = {**event(), "after_estimate": True, "cursor_after": -1,
            "target_budget": "private", "persistent": "private", "summary_calls": float("inf")}
    stored = persist(patch_repository(SOURCE), data).rows[0]
    assert not {"after_estimate", "cursor_after", "target_budget", "persistent", "summary_calls"} & stored.keys()


def test_missing_run_still_prevents_event_insertion():
    engine = persist(patch_repository(SOURCE), event(), exists=False)
    assert not engine.rows and engine.commits == 0


def test_repository_metrics_reach_acceptance_analysis_and_restart_checks():
    before = {"available": True, "exists": True, "version": 3, "cursor": 12, "summary_sha256": "hash"}
    stored = persist(patch_repository(SOURCE), event()).rows[0]
    row = {"round": 22, "phase": "after", "status": 200, "context": stored, "full_history_estimate": 10000}
    result = analyze([row], False, {"saved_before_restart": before, "read_before_first_after_message": dict(before)})
    assert result["events_with_numeric_metrics"] == 1
    assert result["summary_trigger_verified"] is True and result["history_budget_pass"] is True
    assert result["restart_summary_recovery"] is True and result["summary_calls"] == 1
    assert result["history_size_comparisons"][0]["estimated_reduction_ratio"] == .7
    assert result["actual_provider_tokens"] is None and not result["billing_savings_verified"]


def test_partial_marker_and_unknown_layout_are_rejected():
    with pytest.raises(ValueError):
        patch_repository(MARKER + "\n" + SOURCE)
    with pytest.raises(ValueError):
        patch_repository("class Repository: pass\n")


def test_only_capture_assignment_and_payload_reference_change():
    original = ast.parse(SOURCE)
    fixed = ast.parse(patch_repository(SOURCE))
    method = fixed.body[1].body[0]
    capture = method.body.pop(1)
    assert isinstance(capture, ast.Assign) and capture.targets[0].id == "context_metrics"
    payload = next(node.value for node in ast.walk(method) if isinstance(node, ast.keyword) and node.arg == "payload")
    for index, (key, value) in enumerate(zip(payload.keys, payload.values)):
        if key is None and isinstance(value, ast.Name) and value.id == "context_metrics":
            payload.values[index] = capture.value
    assert ast.dump(fixed, include_attributes=False) == ast.dump(original, include_attributes=False)
