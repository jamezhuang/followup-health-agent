import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.chat_integration import router, search_local_with_events
from app.providers import ProviderError


class Events:
    def __init__(self):
        self.rows = []

    async def write(self, run_id, stage, **details):
        self.rows.append({"run_id": run_id, "stage": stage, **details})


def test_diagnostics_requires_dev_token_and_returns_no_model_secrets():
    app = FastAPI()
    app.state.settings = SimpleNamespace(mode="live", dev_token="test-dev-token",
                                         deepseek_key="do-not-return-this")
    app.state.agent = SimpleNamespace(search=SimpleNamespace(planning_enabled=True, rerank_enabled=True,
        search=lambda: None, search_detailed=lambda: None))
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/dev/chat-integration").status_code == 403
        assert client.get("/dev/chat-integration", headers={"X-Dev-Token": "wrong"}).status_code == 403
        response = client.get("/dev/chat-integration", headers={"X-Dev-Token": "test-dev-token"})
    assert response.status_code == 200
    assert response.json()["planning_enabled"] is True
    assert response.json()["rerank_enabled"] is True
    assert "do-not-return-this" not in response.text
    assert "test-dev-token" not in response.text


def test_empty_dev_token_cannot_enable_diagnostics():
    app = FastAPI()
    app.state.settings = SimpleNamespace(mode="live", dev_token="")
    app.state.agent = SimpleNamespace(search=None)
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/dev/chat-integration").status_code == 403


def test_plain_search_is_reported_as_not_planned():
    app = FastAPI()
    app.state.settings = SimpleNamespace(mode="live", dev_token="token")
    app.state.agent = SimpleNamespace(search=SimpleNamespace(search=lambda: None))
    app.include_router(router)
    with TestClient(app) as client:
        data = client.get("/dev/chat-integration", headers={"X-Dev-Token": "token"}).json()
    assert not data["planning_enabled"] and not data["rerank_enabled"]
    assert data["search_available"] and not data["detailed_search_available"]


def test_detailed_search_records_actual_strategy_and_fallback_without_query_or_text():
    values = [SimpleNamespace(id="A", text="private-source-text")]
    calls = []

    class Search:
        async def search_detailed(self, query, top_k):
            calls.append((query, top_k))
            return {"results": values, "strategy": "planned", "branches": [
                {"rerank": {"enabled": True, "status": "ok"}, "selected": [1]},
                {"rerank": {"enabled": True, "status": "fallback", "error_code": "invalid_schema"}, "selected": []},
            ]}

        async def search(self, *args):
            raise AssertionError("不能再次检索，避免重复模型调用")

    events = Events()
    result = asyncio.run(search_local_with_events(Search(), events, "run", "patient-query-not-for-logs", 3))
    assert result is values
    assert calls == [("patient-query-not-for-logs", 3)]
    assert any(row["stage"] == "query_planning" and row["status"] == "ok" for row in events.rows)
    assert any(row["stage"] == "rerank" and row["status"] == "fallback" for row in events.rows)
    assert events.rows[-1]["source_count"] == 1
    assert "private-source-text" not in str(events.rows)
    assert "patient-query-not-for-logs" not in str(events.rows)


def test_legacy_search_still_returns_original_sources():
    values = ["source"]

    class Search:
        async def search(self, query, top_k):
            return values
    events = Events()
    assert asyncio.run(search_local_with_events(Search(), events, "run", "query", 3)) is values
    assert any(row.get("mode") == "legacy" for row in events.rows)


def test_provider_failure_remains_failure_with_original_error_code():
    class Search:
        async def search_detailed(self, *args):
            raise ProviderError("embedding_format", "模拟向量错误")
    events = Events()
    with pytest.raises(ProviderError) as caught:
        asyncio.run(search_local_with_events(Search(), events, "run", "query", 3))
    assert caught.value.code == "embedding_format"
    assert events.rows[-1]["error_code"] == "embedding_format"


def test_planner_fallback_is_not_logged_as_successful_planning():
    class Search:
        async def search_detailed(self, *args):
            return {"results": [], "strategy": "baseline_fallback", "branches": []}
    events = Events()
    assert asyncio.run(search_local_with_events(Search(), events, "run", "query", 3)) == []
    assert any(row["stage"] == "query_planning" and row["status"] == "fallback" for row in events.rows)
