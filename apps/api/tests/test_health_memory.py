import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select, update

from app.workspace.health_memory import (ExtractRequest, Extraction, MemoryError, MemoryRepository,
                                        ProfileWrite, ResolveRequest, comparison, extract_document, merge_profile_facts)
from app.workspace.health_memory_integration import (attach_memory_agent, enrich, install_health_memory,
                                                    model_context, validate_document)
from app.workspace.models import Document
from app.workspace.tools import identity
from scripts.apply_health_memory import patch

OWNER = "alice"
DOC = "a" * 32
PIECES = [{"location": "段落1", "text": "本人年龄：28岁；既往史：糖尿病；过敏史：青霉素过敏。\n2026-10-01 检查：血糖 6.1 mmol/L。"}]


class DocRepo:
    def __init__(self, engine):
        self.engine = engine

    def document(self, owner, doc_id):
        with self.engine.connect() as db:
            row = db.execute(select(Document.__table__).where(Document.owner == owner,
                             Document.id == doc_id, Document.status != "deleted")).mappings().first()
        if not row:
            raise MemoryError("document_not_found", 404)
        return dict(row)


@pytest.fixture
def engine(tmp_path):
    value = create_engine("sqlite:///" + str(tmp_path / "memory.sqlite3"),
                          connect_args={"check_same_thread": False})
    Document.__table__.create(value)
    with value.begin() as db:
        db.execute(insert(Document).values(id=DOC, owner=OWNER, name="report.md", suffix=".md",
            storage_key="test.md", digest="test", size=90, status="ready", revision=2, pieces=PIECES,
            created_at="2026-10-01", updated_at="2026-10-01", error_code="", chunk_count=1))
    yield value
    value.dispose()


@pytest.fixture
def memory(engine):
    repo = MemoryRepository(engine)
    repo.initialize()
    repo.seed(OWNER, {"age": {"value": "30岁"}, "allergies": {"value": "无药物过敏史"}})
    return repo


def fact(field="allergies", value="青霉素过敏", **extra):
    return {"field": field, "value": value, "label": "", "unit": "", "effective_date": None,
            "piece_index": 0, "quote": value, "location": "段落1", **extra}


def propose(memory, engine, values=None):
    return memory.propose(OWNER, DocRepo(engine).document(OWNER, DOC), values or [fact()])


def resolve(memory, snapshot, action="replace", current=True, owner=OWNER):
    return memory.resolve(owner, snapshot["pending"][0]["id"], ResolveRequest(
        expected_version=snapshot["version"], action=action, confirm_current=current))


def test_candidates_do_not_overwrite_profile(memory, engine):
    snap = propose(memory, engine)
    assert snap["fields"]["allergies"]["value"] == "无药物过敏史"
    assert len(snap["pending"]) == 1
    view = enrich(memory, SimpleNamespace(repo=DocRepo(engine)), OWNER)
    assert view["pending"][0]["comparison"] == "conflict"


def test_owner_isolation(memory, engine):
    snap = propose(memory, engine)
    assert memory.snapshot("bob")["pending"] == []
    assert memory.snapshot("bob")["fields"] == {}
    with pytest.raises(MemoryError) as error:
        resolve(memory, snap, owner="bob")
    assert error.value.status == 404
    assert memory.versions("bob")["items"] == []


def test_confirm_replaces_and_preserves_version(memory, engine):
    snap = propose(memory, engine)
    updated = resolve(memory, snap)
    assert updated["fields"]["allergies"]["value"] == "青霉素过敏"
    assert updated["fields"]["allergies"]["provenance"][0]["quote"] == "青霉素过敏"
    event = memory.versions(OWNER)["items"][0]
    assert event["before"]["allergies"]["value"] == "无药物过敏史"
    assert event["after"]["allergies"]["value"] == "青霉素过敏"


def test_current_confirmation_is_required(memory, engine):
    snap = propose(memory, engine)
    with pytest.raises(MemoryError) as error:
        resolve(memory, snap, current=False)
    assert error.value.code == "memory_current_confirmation_required"
    assert memory.snapshot(OWNER)["fields"] == snap["fields"]


def test_compare_and_swap_rejects_old_browser(memory, engine):
    snap = propose(memory, engine)
    memory.save_profile(OWNER, ProfileWrite(expected_version=snap["version"], fields={"age": "31岁"}))
    with pytest.raises(MemoryError) as error:
        resolve(memory, snap)
    assert error.value.code == "memory_version_conflict"
    assert memory.snapshot(OWNER)["fields"]["age"]["value"] == "31岁"


def test_replayed_confirmation_is_idempotent(memory, engine):
    snap = propose(memory, engine)
    updated = resolve(memory, snap)
    assert resolve(memory, snap) == {"replayed": True}
    assert memory.snapshot(OWNER)["version"] == updated["version"]
    with pytest.raises(MemoryError):
        resolve(memory, snap, action="reject")


def test_repeated_extraction_deduplicates(memory, engine):
    first = propose(memory, engine)
    second = propose(memory, engine)
    assert len(second["pending"]) == 1
    assert second["version"] == first["version"]


@pytest.mark.parametrize("action", ["keep", "reject", "record"])
def test_non_overwrite_decisions(memory, engine, action):
    snap = propose(memory, engine)
    updated = resolve(memory, snap, action=action)
    assert updated["fields"] == snap["fields"]
    assert not updated["pending"]
    assert bool(updated["observations"]) == (action == "record")


def test_append_complementary_history_only_after_confirmation(memory, engine):
    v = memory.snapshot(OWNER)["version"]
    memory.save_profile(OWNER, ProfileWrite(expected_version=v, fields={"history": "高血压"}))
    snap = propose(memory, engine, [fact("history", "糖尿病")])
    assert snap["fields"]["history"]["value"] == "高血压"
    updated = resolve(memory, snap, action="append")
    assert updated["fields"]["history"]["value"] == "高血压；糖尿病"


def test_age_cannot_union(memory, engine):
    snap = propose(memory, engine, [fact("age", "28岁")])
    with pytest.raises(MemoryError) as error:
        resolve(memory, snap, action="append")
    assert error.value.code == "memory_age_cannot_append"


def test_contradictory_allergies_cannot_union(memory, engine):
    snap = propose(memory, engine)
    with pytest.raises(MemoryError) as error:
        resolve(memory, snap, action="append")
    assert error.value.code == "memory_conflict_cannot_append"


def test_observation_cannot_replace_profile(memory, engine):
    snap = propose(memory, engine, [fact("observation", "6.1", label="血糖", unit="mmol/L", effective_date="2026-10-01")])
    with pytest.raises(MemoryError) as error:
        resolve(memory, snap)
    assert error.value.code == "memory_observation_append_only"
    updated = resolve(memory, snap, action="record")
    assert updated["observations"][0]["effective_date"] == "2026-10-01"


@pytest.mark.parametrize("new_date,value,expected", [
    ("2026-10-02", "7.2", "dated_record"), ("2026-10-01", "7.2", "conflict"),
    ("2026-10-01", "6.1", "same_record"), (None, "7.2", "undated_record")])
def test_observation_comparison(new_date, value, expected):
    rows = [fact("observation", "6.1", label="血糖", unit="mmol/L", effective_date="2026-10-01")]
    incoming = fact("observation", value, label="血糖", unit="mmol/L", effective_date=new_date)
    assert comparison(incoming, {}, rows) == expected


def test_historical_age_not_current_by_upload_date():
    current = {"age": {"value": "30岁", "effective_date": "2026-10-01"}}
    assert comparison(fact("age", "28岁", effective_date="2024-10-01"), current, []) == "historical"


def test_deleted_or_edited_document_cannot_be_confirmed(memory, engine):
    snap = propose(memory, engine)
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(revision=3))
    with pytest.raises(MemoryError) as error:
        memory.resolve(OWNER, snap["pending"][0]["id"], ResolveRequest(
            expected_version=snap["version"], action="replace", confirm_current=True),
            lambda db, row: validate_document(db, OWNER, row))
    assert error.value.code == "memory_document_stale"
    view = enrich(memory, SimpleNamespace(repo=DocRepo(engine)), OWNER)
    assert view["pending"][0]["stale"]
    assert model_context(view)["ambiguous_fields"] == []


def test_seed_does_not_reimport_old_values(memory):
    version = memory.snapshot(OWNER)["version"]
    memory.save_profile(OWNER, ProfileWrite(expected_version=version, fields={"age": "32岁"}))
    memory.seed(OWNER, {"age": {"value": "18岁"}})
    assert memory.snapshot(OWNER)["fields"]["age"]["value"] == "32岁"


def test_restore_after_restart(memory, engine):
    propose(memory, engine)
    second = MemoryRepository(engine)
    second.initialize()
    assert second.snapshot(OWNER) == memory.snapshot(OWNER)


@pytest.mark.parametrize("fault,code", [("public", "memory_not_personal"),
    ("unknown", "memory_not_personal"), ("quote", "memory_extraction_unsupported"),
    ("value", "memory_extraction_unsupported"), ("piece", "memory_extraction_unsupported"),
    ("unit", "memory_extraction_unsupported"),
    ("provider", "memory_extraction_failed")])
def test_extraction_rejects_unsupported_information(engine, fault, code):
    class LLM:
        async def structured(self, schema, prompt, data):
            if fault == "provider":
                raise RuntimeError("private body must not leak")
            row = {"field": "allergies", "value": "青霉素过敏", "quote": "过敏史：青霉素过敏", "piece_index": 0}
            if fault == "quote": row["quote"] = "不存在的原文"
            if fault == "value": row["value"] = "已确诊肺炎"
            if fault == "piece": row["piece_index"] = 9
            if fault == "unit": row["unit"] = "mg"
            if fault == "date": row["effective_date"] = "2020-01-01"
            return schema(scope=fault if fault in {"public", "unknown"} else "personal", facts=[row])
    documents = SimpleNamespace(repo=DocRepo(engine), client=LLM())
    with pytest.raises(MemoryError) as error:
        asyncio.run(extract_document(documents, OWNER, DOC, ExtractRequest(revision=2, subject_confirmed=True)))
    assert error.value.code == code


def test_extraction_validates_literal_quote_and_explicit_date(engine):
    class LLM:
        async def structured(self, schema, prompt, data):
            return schema(scope="personal", facts=[{"field": "age", "value": "28岁", "quote": "本人年龄：28岁", "piece_index": 0}])
    documents = SimpleNamespace(repo=DocRepo(engine), client=LLM())
    _, facts, coverage = asyncio.run(extract_document(documents, OWNER, DOC, ExtractRequest(
        revision=2, subject_confirmed=True, effective_date="2024-10-01")))
    assert facts[0]["effective_date"] == "2024-10-01"
    assert facts[0]["location"] == "段落1"
    assert not coverage["partial"]


def run_extraction(engine, rows, pieces=None, second=None, repair_error=False):
    if pieces is not None:
        with engine.begin() as db:
            db.execute(update(Document).where(Document.id == DOC).values(pieces=pieces))
    class LLM:
        calls = 0
        async def structured(self, schema, prompt, data):
            self.calls += 1
            if self.calls > 1 and repair_error:
                raise RuntimeError("private provider response")
            return schema(scope="personal", facts=rows if self.calls == 1 or second is None else second)
    llm = LLM()
    result = asyncio.run(extract_document(SimpleNamespace(repo=DocRepo(engine), client=llm), OWNER, DOC,
                         ExtractRequest(revision=2, subject_confirmed=True)))
    return result, llm.calls


def test_markdown_age_quote_recovers_literal_source(engine):
    pieces = [{"location":"标题1", "text":"**年龄**： 28岁\n**检查日期**：2026-10-01"}]
    (_, rows, info), calls = run_extraction(engine,
        [{"field":"age", "value":"28岁", "quote":"年龄：28岁", "piece_index":0}], pieces)
    assert rows[0]["value"] == "28岁"
    assert rows[0]["quote"] in pieces[0]["text"]
    assert "**" in rows[0]["quote"]
    assert info["accepted_count"] == 1 and calls == 1


def test_newlines_restore_original_quote_without_inventing_words(engine):
    pieces = [{"location":"段落1", "text":"过敏史：青霉素\n过敏。"}]
    (_, rows, _), _ = run_extraction(engine,
        [{"field":"allergies", "value":"青霉素过敏", "quote":"过敏史：青霉素过敏", "piece_index":0}], pieces)
    assert rows[0]["quote"] in pieces[0]["text"]
    assert rows[0]["value"] in rows[0]["quote"]


def test_check_metrics_are_ignored_without_repairing_or_losing_age(engine):
    rows = [{"field":"age", "value":"28岁", "quote":"本人年龄：28岁", "piece_index":0},
            {"field":"observation", "label":"空腹血糖", "value":"6.1", "quote":"血糖 6.1 mmol/L", "piece_index":0}]
    (_, facts, info), calls = run_extraction(engine, rows)
    assert len(facts) == 1 and facts[0]["field"] == "age"
    assert not info["rejected"] and not info["repair_attempted"]
    assert calls == 1


def test_one_repair_can_fix_nonliteral_quote(engine):
    bad = [{"field":"age", "value":"28岁", "quote":"年龄28岁，不存在的结论", "piece_index":0}]
    good = [{"field":"age", "value":"28岁", "quote":"本人年龄：28岁", "piece_index":0}]
    (_, facts, info), calls = run_extraction(engine, bad, second=good)
    assert facts[0]["value"] == "28岁"
    assert calls == 2 and info["repair_attempted"] and not info["rejected_count"]


def test_repair_unavailable_keeps_valid_candidate(engine):
    rows = [{"field":"age", "value":"28岁", "quote":"本人年龄：28岁", "piece_index":0},
            {"field":"history", "value":"肺炎", "quote":"已确诊肺炎", "piece_index":0}]
    (_, facts, info), calls = run_extraction(engine, rows, repair_error=True)
    assert len(facts) == 1 and facts[0]["value"] == "28岁"
    assert info["repair_failed"] and calls == 2


def test_unverified_date_is_omitted_without_rejecting_age(engine):
    (_, rows, info), calls = run_extraction(engine,
        [{"field":"age", "value":"28岁", "quote":"本人年龄：28岁", "piece_index":0, "effective_date":"2020-01-01"}])
    assert rows[0]["effective_date"] is None
    assert info["warnings"][0]["reason"] == "date_omitted"
    assert calls == 1


def test_explicit_unique_header_date_has_separate_source(engine):
    pieces = [{"location":"标题1", "text":"**检查日期**：2026-10-1"},
              {"location":"结果1", "text":"年龄：28岁"}]
    (_, rows, info), _ = run_extraction(engine,
        [{"field":"age", "value":"28岁",
          "quote":"年龄：28岁", "piece_index":1, "effective_date":"2026-10-01"}], pieces)
    assert rows[0]["date_source"]["piece_index"] == 0
    assert rows[0]["date_source"]["quote"] in pieces[0]["text"]
    assert not info["warnings"]


def test_conflicting_header_dates_are_not_attached(engine):
    pieces = [{"location":"标题1", "text":"检查日期：2026-10-01\n检查日期：2026-10-02"},
              {"location":"结果1", "text":"年龄：28岁"}]
    (_, rows, info), _ = run_extraction(engine,
        [{"field":"age", "value":"28岁",
          "quote":"年龄：28岁", "piece_index":1, "effective_date":"2026-10-01"}], pieces)
    assert rows[0]["effective_date"] is None and info["warnings"]


def test_short_quote_cannot_hide_negation(engine):
    with pytest.raises(MemoryError) as error:
        run_extraction(engine, [{"field":"history", "value":"糖尿病", "quote":"糖尿病", "piece_index":0}],
            [{"location":"段落1", "text":"既往史：否认糖尿病。"}])
    assert error.value.diagnostics["rejected"][0]["reason"] == "negation_lost"


def test_negation_on_another_line_does_not_reject_positive_history(engine):
    (_, rows, _), _ = run_extraction(engine,
        [{"field":"history", "value":"糖尿病", "quote":"既往史：糖尿病", "piece_index":0}],
        [{"location":"段落1", "text":"无过敏史\n既往史：糖尿病"}])
    assert rows[0]["value"] == "糖尿病"


def test_wrapped_negation_cannot_be_lost(engine):
    with pytest.raises(MemoryError) as error:
        run_extraction(engine, [{"field":"history", "value":"糖尿病", "quote":"糖尿病", "piece_index":0}],
            [{"location":"段落1", "text":"既往史：否认\n糖尿病。"}])
    assert error.value.diagnostics["rejected"][0]["reason"] == "negation_lost"


def test_api_extraction_error_explains_field_without_leaking_values(engine, caplog):
    app = make_app(engine)
    class LLM:
        async def structured(self, schema, prompt, data):
            return schema(scope="personal", facts=[{"field":"age", "value":"99岁", "quote":"秘密患者99岁", "piece_index":0}])
    app.state.workspace_documents.client = LLM()
    with TestClient(app) as client:
        client.cookies.set("carebrief_login", OWNER)
        result = client.post(f"/workspace/documents/{DOC}/health-memory", json={"revision":2,"subject_confirmed":True})
        assert result.status_code == 422
        assert "年龄" in result.json()["detail"]
        assert result.json()["diagnostics"]["repair_attempted"]
        assert "99岁" not in result.text and "秘密患者" not in caplog.text
        assert client.get("/profile").json()["fields"]["age"]["value"] == "30岁"


def test_api_partial_success_proposes_age_without_overwriting_profile(engine):
    app = make_app(engine)
    class LLM:
        async def structured(self, schema, prompt, data):
            return schema(scope="personal", facts=[{"field":"age", "value":"28岁", "quote":"本人年龄：28岁", "piece_index":0},
                {"field":"history", "value":"肺炎", "quote":"已确诊肺炎", "piece_index":0}])
    app.state.workspace_documents.client = LLM()
    with TestClient(app) as client:
        client.cookies.set("carebrief_login", OWNER)
        result = client.post(f"/workspace/documents/{DOC}/health-memory", json={"revision":2,"subject_confirmed":True})
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["extraction"]["accepted_count"] == 1
        assert data["pending"][0]["value"] == "28岁"
        assert data["fields"]["age"]["value"] == "30岁"


def make_app(engine):
    app = FastAPI()
    class Store:
        def user(self, token):
            return {"id": token} if token in {OWNER, "bob"} else None
    class Agent:
        async def run(self, message, history, facts, run_id, memory_context=None, confirmed_reports=None):
            self.context = memory_context
            return {"summary": "test"}, {**facts, "allergies": "错误猜测"}
    app.state.store = Store()
    app.state.agent = Agent()
    app.state.workspace_documents = SimpleNamespace(repo=DocRepo(engine), client=None)
    @app.get("/profile")
    async def legacy(request: Request):
        return {"fields": {"age": {"value": "30岁", "source": "user", "confirmed_at": "2026-10-01"}}}
    @app.post("/sessions/{sid}/messages")
    async def message(sid: str):
        return {"ok": True}
    install_health_memory(app)
    return app


def test_api_profile_import_auth_and_version_guard(engine):
    app = make_app(engine)
    with TestClient(app) as client:
        assert client.get("/profile").status_code == 401
        client.cookies.set("carebrief_login", OWNER)
        snap = client.get("/workspace/health-memory").json()
        assert snap["fields"]["age"]["value"] == "30岁"
        saved = client.put("/profile", json={"expected_version": snap["version"], "fields": {"age": "31岁"}})
        assert saved.status_code == 200, saved.text
        stale = client.put("/profile", json={"expected_version": snap["version"], "fields": {"age": "20岁"}})
        assert stale.status_code == 409
        assert client.post("/sessions/test/memory", json={"field": "age", "value": "99岁"}).status_code == 409


def test_api_cross_owner_candidate_denied(engine):
    app = make_app(engine)
    with TestClient(app) as client:
        memory = app.state.health_memory
        memory.seed(OWNER, {})
        snap = propose(memory, engine)
        client.cookies.set("carebrief_login", "bob")
        response = client.post("/workspace/health-memory/candidates/" + snap["pending"][0]["id"] + "/resolve",
                   json={"expected_version": 1, "action": "replace", "confirm_current": True})
        assert response.status_code == 404


def test_agent_receives_fresh_context_and_masks_conflict(memory, engine):
    propose(memory, engine)
    class Agent:
        async def run(self, message, history, facts, run_id, memory_context=None):
            assert "allergies" not in facts
            assert "allergies" not in memory_context
            assert memory_context["health_memory"]["ambiguous_fields"] == ["allergies"]
            return {}, {"allergies": "classifier guess", "symptom": "咳嗽"}
    agent = Agent()
    attach_memory_agent(agent, memory, SimpleNamespace(repo=DocRepo(engine)))
    async def execute():
        token = identity.set((OWNER, "session"))
        try:
            return await agent.run("test", [], {"allergies": "旧值"}, "run", memory_context={"allergies": {"value": "旧值"}})
        finally:
            identity.reset(token)
    _, facts = asyncio.run(execute())
    assert facts == {"symptom": "咳嗽"}


def test_installer_is_idempotent_and_preserves_other_hooks():
    text = "def install_workspace(app):\n    x = 'keep'\n    app.include_router(build_router())\n    return x\n"
    result, changed = patch(text)
    assert changed and "x = 'keep'" in result
    assert result.index("install_health_memory(app)") < result.index("return x")
    assert patch(result) == (result, False)


def test_version_history_is_paginated(memory):
    for i in range(25):
        memory.save_profile(OWNER, ProfileWrite(expected_version=memory.snapshot(OWNER)["version"], fields={"age": str(i)}))
    first = memory.versions(OWNER)
    second = memory.versions(OWNER, first["next_before"])
    assert len(first["items"]) == 20
    assert not ({v["version"] for v in first["items"]} & {v["version"] for v in second["items"]})


def test_two_writers_only_one_can_commit(memory):
    from concurrent.futures import ThreadPoolExecutor
    version = memory.snapshot(OWNER)["version"]
    def save(value):
        try:
            memory.save_profile(OWNER, ProfileWrite(expected_version=version, fields={"age": value}))
            return "ok"
        except MemoryError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["31岁", "32岁"]))
    assert sorted(results) == ["memory_version_conflict", "ok"]


def test_pending_budget_rolls_back_whole_proposal(memory, engine):
    propose(memory, engine, [fact("history", str(i)) for i in range(100)])
    version = memory.snapshot(OWNER)["version"]
    with pytest.raises(MemoryError) as error:
        propose(memory, engine, [fact("history", "another")])
    assert error.value.code == "memory_pending_limit"
    assert len(memory.snapshot(OWNER)["pending"]) == 100
    assert memory.snapshot(OWNER)["version"] == version


def test_model_context_does_not_include_all_provenance_quotes(memory, engine):
    snapshot = enrich(memory, SimpleNamespace(repo=DocRepo(engine)), OWNER)
    snapshot["fields"]["age"]["provenance"] = [{"quote": "sensitive-long-body" * 1000}]
    packet = model_context(snapshot)
    assert "provenance" not in packet["fields"]["age"]
    assert "sensitive-long-body" not in str(packet)


def test_record_conflict_remains_marked_after_saving_both(memory, engine):
    first = propose(memory, engine, [fact("observation", "6.1", label="血糖", unit="mmol/L", effective_date="2026-10-01")])
    resolve(memory, first, action="record")
    second = propose(memory, engine, [fact("observation", "7.2", label="血糖", unit="mmol/L", effective_date="2026-10-01")])
    resolve(memory, second, action="record")
    packet = model_context(enrich(memory, SimpleNamespace(repo=DocRepo(engine)), OWNER))
    assert all(row["record_conflict"] for row in packet["observations"])


def test_first_chat_imports_live_profile_before_using_empty_session_snapshot(engine):
    app = make_app(engine)
    with TestClient(app) as client:
        client.cookies.set("carebrief_login", OWNER)
        assert client.post("/sessions/new/messages", json={"message": "你好"}).status_code == 200
        assert app.state.health_memory.snapshot(OWNER)["fields"]["age"]["value"] == "30岁"


def test_negative_clause_cannot_become_positive_disease(engine):
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(pieces=[{"location": "段落1", "text": "既往史：无糖尿病史"}]))
    class LLM:
        async def structured(self, schema, prompt, data):
            return schema(scope="personal", facts=[{"field": "history", "value": "糖尿病", "quote": "无糖尿病史", "piece_index": 0}])
    with pytest.raises(MemoryError) as error:
        asyncio.run(extract_document(SimpleNamespace(repo=DocRepo(engine), client=LLM()),
                    OWNER, DOC, ExtractRequest(revision=2, subject_confirmed=True)))
    assert error.value.code == "memory_extraction_unsupported"


def test_new_revision_retires_30_candidate_and_proposes_28(memory, engine):
    first = propose(memory, engine, [fact("age", "30岁")])
    old_id = first["pending"][0]["id"]
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(revision=3,
                   pieces=[{"location": "段落1", "text": "本人年龄：28岁"}]))
    document = DocRepo(engine).document(OWNER, DOC)
    result = memory.propose(OWNER, document, [fact("age", "28岁")],
                           lambda db, row: validate_document(db, OWNER, row))
    assert [(row["value"], row["document_revision"]) for row in result["pending"]] == [("28岁", 3)]
    assert result["fields"]["age"]["value"] == "30岁"
    assert memory.candidate(OWNER, old_id)["status"] == "superseded"
    assert memory.candidate(OWNER, old_id)["fact"]["quote"] == "30岁"


def test_extraction_reads_latest_confirmed_pieces_not_old_quote(engine):
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(revision=3,
                   pieces=[{"location": "段落1", "text": "本人年龄：28岁"}]))
    class LLM:
        async def structured(self, schema, prompt, data):
            assert data["pieces"][0]["text"] == "本人年龄：28岁"
            return schema(scope="personal", facts=[{"field": "age", "value": "28岁", "quote": "本人年龄：28岁", "piece_index": 0}])
    document, facts, _ = asyncio.run(extract_document(SimpleNamespace(repo=DocRepo(engine), client=LLM()),
                                   OWNER, DOC, ExtractRequest(revision=3, subject_confirmed=True)))
    assert document["revision"] == 3
    assert facts[0]["value"] == "28岁"


def test_old_confirmed_age_is_preserved_until_user_confirms_new(memory, engine):
    old = propose(memory, engine, [fact("age", "30岁")])
    resolve(memory, old)
    old_id = old["pending"][0]["id"]
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(revision=3))
    new = memory.propose(OWNER, DocRepo(engine).document(OWNER, DOC), [fact("age", "28岁")])
    assert memory.candidate(OWNER, old_id)["status"] == "accepted"
    assert new["fields"]["age"]["value"] == "30岁"
    confirmed = resolve(memory, new)
    assert confirmed["fields"]["age"]["value"] == "28岁"


def test_new_empty_extraction_also_retires_stale_candidates(memory, engine):
    propose(memory, engine)
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(revision=3))
    result = memory.propose(OWNER, DocRepo(engine).document(OWNER, DOC), [])
    assert not result["pending"]
    assert result["fields"]["allergies"]["value"] == "无药物过敏史"


def test_new_revision_does_not_retire_other_document_or_owner(memory, engine):
    document = DocRepo(engine).document(OWNER, DOC)
    memory.propose(OWNER, {**document, "id": "b"*32}, [fact("age", "29岁")])
    memory.seed("bob", {})
    memory.propose("bob", document, [fact("age", "40岁")])
    memory.propose(OWNER, document, [fact("age", "30岁")])
    result = memory.propose(OWNER, {**document, "revision": 3}, [fact("age", "28岁")])
    assert {row["value"] for row in result["pending"]} == {"28岁", "29岁"}
    assert memory.snapshot("bob")["pending"][0]["value"] == "40岁"


def test_model_schema_advertises_only_three_profile_fields():
    schema = Extraction.model_json_schema()
    assert schema["$defs"]["ExtractedFact"]["properties"]["field"]["enum"] == ["age", "history", "allergies"]


def test_pathology_and_current_symptoms_do_not_become_profile_fields(engine):
    pieces = [{"location":"临床简要病史", "text":"临床简要病史：患者女性，28岁，因发现左侧乳房肿块1月余，偶伴局部隐痛就诊。无乳头溢液，无乳头皮肤破溃，无明显体重下降。"},
              {"location":"病理结果", "text":"Ki-67(45%+)；HER2(3+)"}]
    rows = [{"field":"age", "value":"28岁", "quote":"患者女性，28岁", "piece_index":0},
            {"field":"history", "value":"无乳头溢液，无乳头皮肤破溃，无明显体重下降", "quote":"无乳头溢液，无乳头皮肤破溃，无明显体重下降", "piece_index":0},
            {"field":"observation", "label":"Ki-67", "value":"45%+", "quote":"Ki-67(45%+)", "piece_index":1},
            {"field":"observation", "label":"HER2", "value":"3+", "quote":"HER2(3+)", "piece_index":1}]
    (_, facts, info), _ = run_extraction(engine, rows, pieces)
    assert [(row["field"], row["value"]) for row in facts] == [("age", "28岁")]
    assert info["rejected"][0]["reason"] == "history_not_explicit"


@pytest.mark.parametrize("field,value,text,accepted", [
    ("history", "发现左侧乳房肿块1月余，偶伴局部隐痛", "临床简要病史：发现左侧乳房肿块1月余，偶伴局部隐痛。", False),
    ("history", "糖尿病", "既往史：糖尿病。", True),
    ("allergies", "青霉素过敏", "过敏史：青霉素过敏。", True),
    ("history", "患糖尿病10年", "本人患糖尿病10年。", True),
    ("history", "糖尿病", "既往史：无重大疾病。家族史：父亲有糖尿病。", False),
    ("age", "45", "Ki-67：45%", False),
    ("allergies", "无乳头溢液", "临床简要病史：无乳头溢液。", False),
])
def test_profile_fields_have_explicit_role_evidence(engine, field, value, text, accepted):
    rows = [{"field":field, "value":value, "quote":value, "piece_index":0}]
    pieces = [{"location":"段落1", "text":text}]
    if accepted:
        (_, facts, info), calls = run_extraction(engine, rows, pieces)
        assert len(facts) == 1 and calls == 1 and not info["rejected"]
    else:
        with pytest.raises(MemoryError) as error:
            run_extraction(engine, rows, pieces)
        assert error.value.code == "memory_extraction_unsupported"


def test_no_past_or_allergy_history_is_not_inferred_as_none(engine):
    (_, facts, info), calls = run_extraction(engine, [],
        [{"location":"段落1", "text":"临床简要病史：乳房肿块1月余。病理：HER2(3+)"}])
    assert facts == [] and info["accepted_count"] == 0 and calls == 1


def test_old_out_of_scope_candidates_are_hidden_and_cannot_be_confirmed(memory, engine):
    snap = propose(memory, engine, [fact("observation", "6.1", label="血糖", unit="mmol/L")])
    cid = snap["pending"][0]["id"]
    data = enrich(memory, SimpleNamespace(repo=DocRepo(engine)), OWNER)
    assert not data["pending"] and data["ignored_pending_count"] == 1
    with pytest.raises(MemoryError) as error:
        memory.resolve(OWNER, cid, ResolveRequest(expected_version=snap["version"], action="record"),
            lambda db, row: validate_document(db, OWNER, row))
    assert error.value.code == "memory_candidate_scope"


def test_old_misclassified_negative_symptoms_are_hidden_and_retired(memory, engine):
    pieces = [{"location":"段落1", "text":"临床简要病史：患者28岁。无乳头溢液，无乳头皮肤破溃，无明显体重下降。"}]
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(pieces=pieces))
    snap = propose(memory, engine, [fact("history", "无乳头溢液，无乳头皮肤破溃，无明显体重下降")])
    cid = snap["pending"][0]["id"]
    data = enrich(memory, SimpleNamespace(repo=DocRepo(engine)), OWNER)
    assert not data["pending"] and data["ignored_pending_count"] == 1
    doc = DocRepo(engine).document(OWNER, DOC)
    result = memory.propose(OWNER, doc, [fact("age", "28岁", quote="患者28岁")], profile_only=True)
    assert [row["field"] for row in result["pending"]] == ["age"]
    assert memory.candidate(OWNER, cid)["status"] == "superseded"
    assert not result["fields"].get("history")


def test_api_ignores_extra_metrics_and_keeps_confirmed_diabetes(engine):
    app = make_app(engine)
    pieces = [{"location":"病史", "text":"临床简要病史：患者28岁，乳房肿块1月余。无乳头溢液。"},
              {"location":"结果", "text":"Ki-67(45%+)，HER2(3+)"}]
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(pieces=pieces))
    class LLM:
        async def structured(self, schema, prompt, data):
            return schema(scope="personal", facts=[{"field":"age", "value":"28岁", "quote":"患者28岁", "piece_index":0},
                {"field":"observation", "label":"HER2", "value":"3+", "quote":"HER2(3+)", "piece_index":1}])
    app.state.workspace_documents.client = LLM()
    with TestClient(app) as client:
        client.cookies.set("carebrief_login", OWNER)
        initial = client.get("/profile").json()
        client.put("/profile", json={"expected_version":initial["version"],"fields":{"history":"糖尿病"}})
        result = client.post(f"/workspace/documents/{DOC}/health-memory", json={"revision":2,"subject_confirmed":True})
        assert result.status_code == 200, result.text
        data = result.json()
        assert len(data["pending"]) == 1 and data["pending"][0]["field"] == "age"
        assert data["fields"]["history"]["value"] == "糖尿病"


def double_age_pieces(engine):
    pieces = [{"location":"段落1", "text":"年龄：25岁"},
              {"location":"段落2", "text":"临床简要病史：患者女性，25岁。"}]
    with engine.begin() as db:
        db.execute(update(Document).where(Document.id == DOC).values(pieces=pieces))
    return pieces


def test_equal_age_in_two_pieces_becomes_one_candidate(engine):
    pieces = double_age_pieces(engine)
    (_, facts, info), _ = run_extraction(engine,
        [{"field":"age", "value":"25岁", "quote":"年龄：25岁", "piece_index":0},
         {"field":"age", "value":"25岁", "quote":"患者女性，25岁", "piece_index":1}], pieces)
    assert len(facts) == 1 and info["accepted_count"] == 1
    assert len(facts[0]["evidence"]) == 2
    for source in facts[0]["evidence"]:
        assert source["quote"] in pieces[source["piece_index"]]["text"]


def test_numeric_age_and_age_with_year_unit_are_equal():
    rows = [fact("age", "25", quote="年龄：25", unit="岁"), fact("age", "25岁", quote="25岁")]
    assert len(merge_profile_facts(rows)) == 1
    assert comparison(rows[0], {"age":{"value":"25岁"}}, []) == "consistent"


@pytest.mark.parametrize("change", ["value", "date", "document", "revision", "field"])
def test_conflicts_and_different_sources_stay_separate(change):
    first = {**fact("age", "25岁"), "document_id":DOC, "document_revision":2}
    second = dict(first)
    second.update({"value":{"value":"28岁"}, "date":{"effective_date":"2026-10-01"},
                   "document":{"document_id":"b"*32}, "revision":{"document_revision":3},
                   "field":{"field":"history"}}[change])
    assert len(merge_profile_facts([first, second])) == 2


@pytest.mark.parametrize("action,status", [("replace","accepted"),("keep","kept"),
                                           ("record","recorded"),("reject","rejected")])
def test_legacy_duplicates_resolve_together_in_one_transaction(memory, engine, action, status):
    double_age_pieces(engine)
    snap = propose(memory, engine, [fact("age", "25岁", quote="年龄：25岁", piece_index=0, location="段落1"),
        fact("age", "25岁", quote="患者女性，25岁", piece_index=1, location="段落2")])
    ids = [row["id"] for row in snap["pending"]]
    assert len(ids) == 2
    view = enrich(memory, SimpleNamespace(repo=DocRepo(engine)), OWNER)
    assert len(view["pending"]) == 1 and len(view["pending"][0]["evidence"]) == 2
    updated = memory.resolve(OWNER, view["pending"][0]["id"],
        ResolveRequest(expected_version=snap["version"], action=action, confirm_current=action=="replace"),
        lambda db, row: validate_document(db, OWNER, row))
    assert not updated["pending"] and updated["version"] == snap["version"] + 1
    assert memory.candidate(OWNER, view["pending"][0]["id"])["status"] == status
    if action == "replace":
        assert updated["fields"]["age"]["value"] == "25岁"
        assert len(updated["fields"]["age"]["provenance"]) == 2
    elif action == "record":
        assert len(updated["observations"]) == 1
        assert len(updated["observations"][0]["evidence"]) == 2
    else:
        assert updated["fields"]["age"]["value"] == "30岁"


def test_reextraction_adds_evidence_to_same_logical_candidate(memory, engine):
    double_age_pieces(engine)
    document = DocRepo(engine).document(OWNER, DOC)
    first = memory.propose(OWNER, document, [fact("age", "25岁", quote="年龄：25岁", piece_index=0, location="段落1")], profile_only=True)
    second = memory.propose(OWNER, document, [fact("age", "25岁", quote="患者女性，25岁", piece_index=1, location="段落2")], profile_only=True)
    assert len(second["pending"]) == 1 and second["pending"][0]["id"] == first["pending"][0]["id"]
    assert len(second["pending"][0]["evidence"]) == 2
    assert second["version"] == first["version"] + 1
    repeated = memory.propose(OWNER, document, [fact("age", "25岁", quote="患者女性，25岁", piece_index=1, location="段落2")], profile_only=True)
    assert repeated["version"] == second["version"]


def test_group_resolution_does_not_affect_other_owner_or_document(memory, engine):
    double_age_pieces(engine)
    document = DocRepo(engine).document(OWNER, DOC)
    snap = memory.propose(OWNER, document, [fact("age", "25岁", quote="年龄：25岁")])
    cid = snap["pending"][0]["id"]
    memory.propose(OWNER, {**document,"id":"b"*32}, [fact("age", "25岁", quote="年龄：25岁")])
    memory.seed("bob", {})
    memory.propose("bob", document, [fact("age", "25岁", quote="年龄：25岁")])
    version = memory.snapshot(OWNER)["version"]
    updated = memory.resolve(OWNER, cid, ResolveRequest(expected_version=version, action="keep"))
    assert len(updated["pending"]) == 1 and updated["pending"][0]["document_id"] == "b"*32
    assert len(memory.snapshot("bob")["pending"]) == 1
