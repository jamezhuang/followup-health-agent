import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.providers import ProviderError
from app.schemas import Source
from scripts.evaluate_planned import run, summary
from scripts.evaluate_rag import Case, retrieval_metrics


def source(identifier):
    return Source(id=identifier, title=identifier, institution="测试机构",
                  url="https://example.com/source", published_at=None,
                  collected_at="2026-10-07", text="这是可以核对的公开正文。",
                  allowed_departments=[], kind="official_summary")


def test_failed_cases_remain_in_metrics_denominator():
    case = Case(id="T", group="test", query="测试", gold_groups=[["A"]], review_points=[])
    successful = {"document_id": "A", "category": "health_general"}
    cases = [
        {"retrieval": {mode: {"metrics": retrieval_metrics(case, [successful])}
                       for mode in ("dense", "keyword", "hybrid", "planned")}},
        {"retrieval": {mode: {"metrics": retrieval_metrics(case, [])}
                       for mode in ("dense", "keyword", "hybrid", "planned")},
         "planned_error_code": "timeout"},
    ]
    result = summary(cases)
    assert result["planned"]["hit"] == {"mean": 0.5, "n": 2}
    assert result["planned_failures"] == 1


def test_runner_compares_old_and_new_with_same_answer_prompt(tmp_path, monkeypatch):
    import app.config
    import app.hybrid
    import app.planned_search
    import app.providers
    import scripts.evaluate_planned as runner

    a, b = source("A"), source("B")
    calls = []
    closed = []

    class FakeClient:
        def __init__(self, settings):
            pass

        async def structured(self, schema, prompt, payload):
            calls.append((prompt, payload))
            return schema(summary="按返回资料回答", abstained=False, claims=[
                {"text": "公开结论", "evidence_id": item["id"], "quote": "可以核对的公开正文"}
                for item in payload["sources"]
            ])

    class Baseline:
        def __init__(self, *args, **kwargs):
            self.keyword = SimpleNamespace(engine=None)
            self.candidates = 10

        async def search_detailed(self, query, top_k):
            return {"dense": [a, b], "keyword": [a], "results": [a], "warnings": []}

        def close(self):
            closed.append("baseline")

    class Planned(Baseline):
        planning_enabled = True

        async def search_detailed(self, query, top_k):
            return {"strategy": "planned", "plan": {"tasks": []}, "branches": [],
                    "results": [a, b], "warnings": []}

        def close(self):
            closed.append("planned")

    class Lookup:
        def __init__(self, engine):
            pass

        def snapshot(self):
            return [{"document_id": identifier, "metadata_revision": "v1"} for identifier in ("A", "B")]

        def resolve(self, sources):
            return [{"document_id": item.id, "category": "health_general", "hospital_id": None,
                     "source": item.model_dump()} for item in sources]

    monkeypatch.setattr(app.config, "Settings", lambda: SimpleNamespace(
        mode="live", embedding_model="fake", dimensions=2, chat_model="fake"))
    monkeypatch.setattr(app.providers, "APIClient", FakeClient)
    monkeypatch.setattr(app.hybrid, "HybridKnowledgeSearch", Baseline)
    monkeypatch.setattr(app.planned_search, "PlannedKnowledgeSearch", Planned)
    monkeypatch.setattr(runner, "CatalogLookup", Lookup)
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"version": "test", "split": "regression", "note": "测试",
                                "cases": [{"id": "T", "group": "combined", "query": "两个主题",
                                           "gold_groups": [["A"], ["B"]], "review_points": ["标准审核点"]}]}), encoding="utf-8")
    output = tmp_path / "report.json"
    args = SimpleNamespace(suite=suite, case_ids=None, limit=None, top_k=3,
                           with_answers=True, out=output)
    assert asyncio.run(run(args)) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["complete"] and report["corpus_unchanged"]
    assert report["summary"]["hybrid"]["group_recall"]["mean"] == 0.5
    assert report["summary"]["planned"]["group_recall"]["mean"] == 1
    assert report["summary"]["answers"]["planned"]["citation_group_coverage"]["mean"] == 1
    assert calls[0][0] == calls[1][0]
    assert all(set(payload) == {"question", "sources"} for _, payload in calls)
    assert closed == ["planned", "baseline"]
