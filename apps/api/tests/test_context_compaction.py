import asyncio
import ast
import json
from types import SimpleNamespace
import pytest
from app.context_compaction import Config, SummaryStore, compact, estimate, Summary
from app.context_compaction_integration import prepare_context, request_key
from scripts.apply_context_compaction import patch

CFG = Config(trigger=2200, target=1500, summary_budget=300, recent_turns=2,
             batch_budget=2200, max_batches=2, timeout=.05)


def history(n=8, prefix="用户"):
    rows = []
    for i in range(n):
        rows += [{"role": "user", "content": prefix + str(i) + "问" * 20},
                 {"role": "assistant", "content": "回答" + "答" * 130}]
    return rows


class Model:
    def __init__(self, error=False, delay=0, output=None):
        self.calls, self.error, self.delay = [], error, delay
        self.output = output or {"topic": "医院科室", "continuity": "用户询问科室，尚未选定医院。", "pending": []}

    async def complete(self, messages, **kwargs):
        self.calls.append(json.loads(messages[-1]["content"]))
        await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError("sk-private-never-log")
        return {"content": json.dumps(self.output, ensure_ascii=False)}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path):
    return SummaryStore(tmp_path / "summary.sqlite3")


def test_small_history_kept_without_llm(store):
    h = history(2)
    llm = Model()
    view, event = run(compact(h, {}, llm, store, ("u", "s"), CFG))
    assert view == h and not llm.calls
    assert store.load(("u", "s")) is None


def test_threshold_incremental_restart_and_budget(store):
    h, llm = history(), Model()
    view, event = run(compact(h, {"age": "28"}, llm, store, ("u", "s"), CFG))
    saved = store.load(("u", "s"))
    assert saved["cursor"] == 12 and event["status"] == "compressed"
    assert view[-4:] == h[-4:] and estimate(view) <= CFG.target
    assert llm.calls[0]["facts"] == {"age": "28"}
    llm.calls.clear()
    restarted = SummaryStore(store.path)
    view2, event2 = run(compact(h, {}, llm, restarted, ("u", "s"), CFG))
    assert not llm.calls and event2["cursor_before"] == 12 and view2 == view
    h += history(3, "新")
    run(compact(h, {}, llm, restarted, ("u", "s"), CFG))
    assert llm.calls[0]["completed_turns"][0] == h[12]
    assert llm.calls[0]["previous_summary"] == saved["summary"]


@pytest.mark.parametrize("model", [Model(error=True), Model(delay=.1), Model(output={"topic":"长"*290}), Model(output={"topic":"", "continuity":"", "pending":[]})])
def test_failure_does_not_advance_and_remains_bounded(store, model):
    view, event = run(compact(history(), {}, model, store, ("u", "s"), CFG))
    assert event["status"] == "fallback" and store.load(("u", "s")) is None
    assert view[-4:] == history()[-4:] and estimate(view) <= CFG.target
    assert "sk-private" not in json.dumps(event)


def test_failure_keeps_previous_summary(store):
    h = history()
    run(compact(h, {}, Model(), store, ("u", "s"), CFG))
    old = store.load(("u", "s"))
    h += history(4, "新")
    view, event = run(compact(h, {}, Model(error=True), store, ("u", "s"), CFG))
    assert store.load(("u", "s")) == old
    assert old["summary"] in view[1]["content"]
    assert event["cursor_after"] == old["cursor"]


def test_owner_and_session_isolation(store):
    run(compact(history(), {}, Model(), store, ("u1", "s"), CFG))
    for key in [("u2", "s"), ("u1", "other")]:
        assert store.load(key) is None
        view, _ = run(compact(history(1, "其他用户"), {}, Model(), store, key, CFG))
        assert not any("历史对话摘要" in x["content"] for x in view)


def test_changed_history_resets_summary(store):
    run(compact(history(), {}, Model(), store, ("u", "s"), CFG))
    view, event = run(compact(history(2, "替换"), {}, Model(), store, ("u", "s"), CFG))
    assert event["reset"] and view == history(2, "替换")


def test_cas_and_delete(store):
    key = ("u", "s")
    assert store.save(key, None, 2, "a", "b", "one")
    assert not store.save(key, None, 4, "a", "c", "stale")
    assert store.save(key, 1, 4, "a", "c", "two")
    assert not store.save(key, 1, 6, "a", "d", "stale")
    assert store.load(key)["summary"] == "two"
    store.delete(key)
    assert store.load(key) is None


def test_tool_scratch_history_not_summarized(store):
    llm = Model()
    h = [{"role":"assistant","content":"call", "tool_calls":[{}]}, {"role":"tool","content":"result"}]
    with pytest.raises(ValueError):
        run(compact(h, {}, llm, store, ("u", "s"), CFG))
    assert not llm.calls


def test_oversize_latest_turn_not_cut_in_half(store):
    h = history() + [{"role":"user","content":"长"*1000}, {"role":"assistant","content":"长"*1000}]
    view, event = run(compact(h, {}, Model(error=True), store, ("u", "s"), CFG))
    assert estimate(view) <= CFG.target
    assert event["omitted_recent_messages"] >= 2
    assert any("上下文容量提示" in x["content"] for x in view)


def test_max_batches_caps_cost(store):
    llm = Model()
    view, event = run(compact(history(10000), {}, llm, store, ("u", "s"), CFG))
    assert len(llm.calls) == CFG.max_batches and event["unsummarized_old_messages"] > 0
    assert estimate(view) <= CFG.target
    assert all(estimate(x["completed_turns"]) <= CFG.batch_budget for x in llm.calls)


def test_integration_unbound_has_no_model_cost(tmp_path, monkeypatch):
    for k, v in {"CONTEXT_TRIGGER":2000, "CONTEXT_TARGET":1200, "CONTEXT_SUMMARY_BUDGET":300,
                 "CONTEXT_BATCH_BUDGET":2000, "CONTEXT_RECENT_TURNS":2}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("CONTEXT_SUMMARY_DB", str(tmp_path / "summary.db"))
    class Events:
        async def write(self, run_id, stage, **data):
            self.data = data
    agent = SimpleNamespace(client=Model(), settings=SimpleNamespace(mode="live"), events=Events())
    view = run(prepare_context(agent, history(), {}, "run"))
    assert not agent.client.calls and not (tmp_path / "summary.db").exists()
    assert agent.events.data["binding"] == "unbound_recent_only"


SOURCE = '''from __future__ import annotations
import time
class Agent:
    @audit
    async def run(
        self,
        message,
        history,
        facts,
        run_id,
        memory_context=None,
        confirmed_reports=None,
    ):
        try:
            model_history = build_history(
                history, max_turns=self.settings.history_max_turns,
                max_chars=self.settings.history_max_chars,
            )
        except HistoryBudgetExceeded:
            raise
        return await self.graph.ainvoke({"history":model_history,"facts":facts})
    async def retrieve(self,state):
        return "preserved"
'''


def test_installer_preserves_decorator_other_methods_and_idempotent():
    revised = patch(SOURCE)
    assert patch(revised) == revised and "@audit" in revised
    assert 'return "preserved"' in revised
    assert "context_key=None" in revised and "await prepare_context" in revised
    assert revised.startswith("from __future__ import annotations")
    ast.parse(revised)


def test_installer_rejects_unknown_structure():
    with pytest.raises(ValueError):
        patch(SOURCE.replace("build_history(", "new_history("))


def test_same_view_reaches_graph_through_installed_method(monkeypatch):
    # Execute just the patched class with actual hook binding; no real network needed.
    source = patch(SOURCE)
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    namespace = {"audit": lambda f:f, "HistoryBudgetExceeded":ValueError}
    received = {}
    async def prepare(*args):
        return [{"role":"user","content":"summary"}]
    namespace["prepare_context"] = prepare
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "patched", "exec"), namespace)
    async def invoke(value):
        received.update(value)
        return value
    agent = namespace["Agent"]()
    agent.graph = SimpleNamespace(ainvoke=invoke)
    run(agent.run("当前问题", history(), {"age":"28"}, "run", context_key=("u", "s")))
    assert received["history"] == [{"role":"user","content":"summary"}]
    assert received["facts"] == {"age":"28"}


def test_database_read_failure_falls_back_without_llm():
    class BrokenStore:
        def load(self, key):
            raise OSError("private-db-path")
    llm = Model()
    view, event = run(compact(history(), {}, llm, BrokenStore(), ("u", "s"), CFG))
    assert not llm.calls and estimate(view) <= CFG.target
    assert event["persistence_error"] and not event["persistent"]


def test_cancellation_never_saves_cursor(store):
    class Cancelled:
        async def complete(self, *args, **kwargs):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(compact(history(), {}, Cancelled(), store, ("u", "s"), CFG))
    assert store.load(("u", "s")) is None


def test_explicit_owner_binding():
    assert request_key(("owner", "session")) == ("owner", "session")
    with pytest.raises(ValueError):
        request_key("session-only")


def test_single_pending_user_is_kept_without_summary(store):
    h = [{"role":"user", "content":"我头晕"}]
    llm = Model()
    view, event = run(compact(h, {}, llm, store, ("u", "s"), CFG))
    assert view == h and event["pending_messages"] == 1
    assert not llm.calls and store.load(("u", "s")) is None


def test_odd_long_history_pending_user_not_in_summary(store):
    h = history() + [{"role":"user", "content":"这个问题尚未回答"}]
    llm = Model()
    view, event = run(compact(h, {}, llm, store, ("u", "s"), CFG))
    assert view[-1] == h[-1] and event["pending_messages"] == 1
    assert all(h[-1] not in call["completed_turns"] for call in llm.calls)
    assert store.load(("u", "s"))["cursor"] == 12
    # Completing the pending turn later keeps cursor continuity.
    h.append({"role":"assistant", "content":"已经回答"})
    run(compact(h, {}, llm, store, ("u", "s"), CFG))
    assert store.load(("u", "s"))["cursor"] >= 12


def test_opening_assistant_with_completed_turns_is_valid(store):
    h = [{"role":"assistant", "content":"欢迎，请描述问题。"}] + history()
    llm = Model()
    view, event = run(compact(h, {}, llm, store, ("u", "s"), CFG))
    saved = store.load(("u", "s"))
    assert saved["cursor"] == 13 and view[-4:] == h[-4:]
    assert llm.calls[0]["completed_turns"][0] == h[0]
    llm.calls.clear()
    _, again = run(compact(h, {}, llm, store, ("u", "s"), CFG))
    assert not again["reset"] and not llm.calls


def test_consecutive_users_and_assistants_are_one_complete_block(store):
    h = [{"role":"user", "content":"上次问题"}, {"role":"user", "content":"补充说明"},
         {"role":"assistant", "content":"回答"}, {"role":"assistant", "content":"追加说明"}]
    h += history()
    llm = Model()
    view, event = run(compact(h, {}, llm, store, ("u", "s"), CFG))
    assert llm.calls[0]["completed_turns"][:4] == h[:4]
    assert view[-4:] == h[-4:] and estimate(view) <= CFG.target


def test_multiple_pending_users_remain_uncompressed(store):
    tail = [{"role":"user", "content":"还没回答1"}, {"role":"user", "content":"还没回答2"}]
    llm = Model()
    view, event = run(compact(history() + tail, {}, llm, store, ("u", "s"), CFG))
    assert view[-2:] == tail and event["pending_messages"] == 2
    assert all(not any(m in call["completed_turns"] for m in tail) for call in llm.calls)


def test_pending_user_survives_summary_failure(store):
    last = {"role":"user", "content":"这个医院呢"}
    view, event = run(compact(history() + [last], {}, Model(error=True), store, ("u", "s"), CFG))
    assert event["status"] == "fallback" and view[-1] == last
    assert estimate(view) <= CFG.target and store.load(("u", "s")) is None


def test_extremely_long_pending_group_remains_bounded(store):
    h = [{"role":"user", "content":"未答" * 100} for _ in range(10000)]
    llm = Model()
    view, event = run(compact(h, {}, llm, store, ("u", "s"), CFG))
    assert not llm.calls and event["pending_messages"] == len(h)
    assert estimate(view) <= CFG.target and event["omitted_recent_messages"] == len(h)


def test_latest_input_deduplicated_without_copying_history(monkeypatch, tmp_path):
    from app.context_compaction_integration import PrefixHistory
    class Events:
        async def write(self, run_id, stage, **data):
            self.data = data
    h = history(1) + [{"role":"user", "content":"新问题"}]
    agent = SimpleNamespace(client=Model(), settings=SimpleNamespace(mode="live"), events=Events())
    monkeypatch.setenv("CONTEXT_SUMMARY_DB", str(tmp_path / "db.sqlite3"))
    result = run(prepare_context(agent, h, {}, "run", context_key=("u", "s"), current_message="新问题"))
    assert result == h[:-1] and len(h) == 3 and agent.events.data["current_input_deduplicated"]
    view = PrefixHistory(h, 2)
    assert view.history is h and view[-1] == h[1]


def test_unrelated_pending_user_not_removed(monkeypatch, tmp_path):
    class Events:
        async def write(self, *args, **data):
            self.data = data
    h = history(1) + [{"role":"user", "content":"上次未完成的问题"}]
    agent = SimpleNamespace(client=Model(), settings=SimpleNamespace(mode="live"), events=Events())
    monkeypatch.setenv("CONTEXT_SUMMARY_DB", str(tmp_path / "db.sqlite3"))
    result = run(prepare_context(agent, h, {}, "run", context_key=("u", "s"), current_message="现在另一个问题"))
    assert result == h and not agent.events.data["current_input_deduplicated"]
