import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.evidence_rerank import RerankGrades, rerank_evidence
from app.hybrid import fuse_rrf
from app.planned_search import PlannedKnowledgeSearch, QueryPlan, SearchTask
from app.providers import APIClient, ProviderError
from app.schemas import Source


def source(identifier, body="测试公开正文可以核对。"):
    return Source(id=identifier, title=identifier, institution="甲医院",
                  url="https://example.com/public", published_at=None,
                  collected_at="2026-10-07", text=body,
                  allowed_departments=[], kind="official_summary")


def task(scope="hospital", query="眼表疾病相关科室"):
    return SearchTask(scope=scope, query=query, hospital_id="H1" if scope == "hospital" else None,
                      hospital_name="甲医院" if scope == "hospital" else None)


class FakeClient:
    def __init__(self, scores=None, error=None, output=None):
        self.scores = scores or {}
        self.error, self.output, self.inputs = error, output, []

    async def structured(self, schema, prompt, payload):
        self.inputs.append(payload)
        if self.error:
            raise self.error
        if self.output is not None:
            return self.output
        return schema(grades=[
            {"source_id": row["source_id"], "relevance": self.scores.get(row["source_id"], 0),
             "quote": row["text"][:6] if self.scores.get(row["source_id"], 0) >= 2 else ""}
            for row in payload["candidates"]
        ])


def test_wrong_department_rrf_consensus_cannot_displace_supported_eye_source():
    eye, endo, resp, gyn, rheum = [source(key) for key in ["EYE", "ENDO", "RESP", "GYN", "RHEUM"]]
    fused = fuse_rrf([[eye, endo, resp], [gyn, endo, rheum, eye]], 5)
    assert fused[0].id == "ENDO"  # 复现同院错误科室超过向量首位的故障。
    client = FakeClient({"EYE": 3, "ENDO": 1, "RESP": 0, "GYN": 0, "RHEUM": 0})
    result = asyncio.run(rerank_evidence(client, "眼睛流泪，甲医院该看什么科？", task(), fused, 3))
    assert [s.id for s in result.sources] == ["EYE"]
    assert result.sources[0].score == next(s.score for s in fused if s.id == "EYE")
    assert result.audit["status"] == "ok"
    assert len(client.inputs[0]["candidates"]) == 5  # 不是先取最终三条再评分。


def test_partial_evidence_is_retained_but_topic_only_is_removed():
    sources = [source(key) for key in ["TOPIC", "PARTIAL", "DIRECT"]]
    result = asyncio.run(rerank_evidence(FakeClient({"TOPIC": 1, "PARTIAL": 2, "DIRECT": 3}),
                                        "公开科普", task("general", "科普"), sources, 3))
    assert [s.id for s in result.sources] == ["DIRECT", "PARTIAL"]


def test_no_support_returns_empty_without_irrelevant_padding():
    result = asyncio.run(rerank_evidence(FakeClient(), "实时挂号", task(), [source("A")], 3))
    assert result.sources == []
    assert result.warnings == ["rerank_no_supporting_source"]


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "missing", "fabricated_quote", "bool_score"])
def test_invalid_scoring_falls_back_instead_of_using_bad_output(kind):
    a, b = source("A"), source("B")
    grades = [{"source_id": "A", "relevance": 3, "quote": a.text[:6]},
              {"source_id": "B", "relevance": 0, "quote": ""}]
    if kind == "unknown":
        grades[1]["source_id"] = "INVENTED"
    elif kind == "duplicate":
        grades[1]["source_id"] = "A"
    elif kind == "missing":
        grades.pop()
    elif kind == "fabricated_quote":
        grades[0]["quote"] = "正文根本没有这段话"
    else:
        grades[0]["relevance"] = True
    result = asyncio.run(rerank_evidence(FakeClient(output={"grades": grades}),
                                        "主题", task(), [a, b], 3))
    assert result.sources == [a, b]
    assert result.audit["status"] == "fallback"
    assert result.warnings[0].startswith("rerank_fallback:")


def test_upstream_failure_preserves_existing_order():
    a, b = source("A"), source("B")
    result = asyncio.run(rerank_evidence(FakeClient(error=ProviderError("provider_http", "上游异常")),
                                        "主题", task(), [a, b], 1))
    assert result.sources == [a]
    assert result.audit["error_code"] == "provider_http"


def test_timeout_is_bounded_and_cancelled():
    class Slow:
        cancelled = False

        async def structured(self, *args):
            try:
                await asyncio.sleep(5)
            finally:
                self.cancelled = True
    client = Slow()
    result = asyncio.run(rerank_evidence(client, "主题", task(), [source("A")], 3, 0.01))
    assert result.audit["error_code"] == "rerank_timeout"
    assert client.cancelled


def test_empty_candidates_do_not_call_model():
    client = FakeClient()
    result = asyncio.run(rerank_evidence(client, "主题", task(), [], 3))
    assert client.inputs == []
    assert result.audit["model_calls"] == 0


def test_existing_api_adapter_accepts_rerank_schema_via_mock_transport():
    calls = []

    def reply(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["response_format"] == {"type": "json_object"}
        assert set(json.loads(body["messages"][1]["content"])) == {"question", "task", "candidates"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"grades": [
                {"source_id": "A", "relevance": 3, "quote": "测试公开正文"}]}, ensure_ascii=False)}}]})

    settings = SimpleNamespace(chat_model="mock-chat", disable_thinking=True,
                               deepseek_base="https://api.example.com", deepseek_key="fake-test-key", timeout=1)
    result = asyncio.run(rerank_evidence(APIClient(settings, httpx.MockTransport(reply)),
                                        "公开主题", task(), [source("A")], 3))
    assert result.audit["status"] == "ok"
    assert len(calls) == 1


def test_payload_has_no_gold_fields_and_quote_must_be_in_presented_excerpt():
    a = source("A", "甲" * 1000 + "只在截断之后出现的支持引文")
    client = FakeClient(output={"grades": [{"source_id": "A", "relevance": 3,
                                          "quote": "只在截断之后出现的支持引文"}]})
    result = asyncio.run(rerank_evidence(client, "查询主题", task(), [a], 3))
    assert set(client.inputs[0]) == {"question", "task", "candidates"}
    assert set(client.inputs[0]["candidates"][0]) == {"source_id", "title", "institution", "text"}
    assert result.audit["truncated_ids"] == ["A"]
    assert result.audit["error_code"] == "rerank_quote_invalid"


class FakeSearch(PlannedKnowledgeSearch):
    def __init__(self, client, enabled):
        self._plan_settings = SimpleNamespace(mode="live")
        self._plan_client = client
        self._planning_enabled = True
        self._rerank_enabled, self._rerank_timeout = enabled, 1
        self.candidates = 10
        self._catalog = SimpleNamespace(hospitals=lambda: [{"id": "H1", "name": "甲医院", "aliases": []}],
            metadata=lambda sources: {s.id: {"category": "health_general" if s.id in {"PUBLIC", "JUNK"}
                                             else "hospital_department", "hospital_id": "H2" if s.id == "FOREIGN" else "H1"}
                                      for s in sources})

    async def _raw_search(self, query, top_k):
        ids = ["PUBLIC", "JUNK"] if query == "干眼科普" else ["ENDO", "EYE", "FOREIGN"]
        items = [source(key) for key in ids]
        return {"dense": items, "keyword": items, "results": items, "warnings": []}


def test_online_path_filters_before_rerank_and_preserves_both_branches():
    class Client(FakeClient):
        async def structured(self, schema, prompt, payload):
            if schema is QueryPlan:
                return QueryPlan(tasks=[task("general", "干眼科普"), task()])
            return await super().structured(schema, prompt, payload)
    client = Client({"PUBLIC": 3, "EYE": 3, "ENDO": 0, "JUNK": 0})
    result = asyncio.run(FakeSearch(client, True).search_detailed("干眼科普与甲医院相关科室", 3))
    assert [s.id for s in result["results"]] == ["PUBLIC", "EYE"]
    assert all(row["source_id"] != "FOREIGN" for payload in client.inputs for row in payload["candidates"])
    assert all(branch["rerank"]["status"] == "ok" for branch in result["branches"])


def test_disabled_path_does_not_consume_rerank_model_call():
    class Client(FakeClient):
        async def structured(self, schema, prompt, payload):
            assert schema is QueryPlan
            return QueryPlan(tasks=[task()])
    client = Client()
    result = asyncio.run(FakeSearch(client, False).search_detailed("甲医院相关科室", 3))
    assert [s.id for s in result["results"]] == ["ENDO", "EYE"]
    assert client.inputs == []
    assert result["branches"][0]["rerank"]["status"] == "disabled"


def test_invalid_config_is_rejected_before_creating_database_engine(monkeypatch):
    monkeypatch.setenv("RAG_RERANK_ENABLED", "maybe")
    with pytest.raises(ValueError, match="RAG_RERANK_ENABLED"):
        PlannedKnowledgeSearch(SimpleNamespace(mode="live"), FakeClient())


@pytest.mark.parametrize("value", ["nan", "inf", "0", "121"])
def test_timeout_config_is_validated_before_database_connection(monkeypatch, value):
    monkeypatch.setenv("RAG_RERANK_ENABLED", "true")
    monkeypatch.setenv("RAG_RERANK_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="RAG_RERANK_TIMEOUT_SECONDS"):
        PlannedKnowledgeSearch(SimpleNamespace(mode="live"), FakeClient())
