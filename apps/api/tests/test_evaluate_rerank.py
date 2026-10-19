import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.providers import ProviderError
from app.schemas import Source
from scripts.evaluate_rag import PROMPT
from scripts.evaluate_rerank import prepare_case, replay_case, run, summary


def source(identifier):
    return Source(id=identifier, title=identifier, institution="甲医院",
                  url="https://example.com/public", published_at=None,
                  collected_at="2026-10-07", text="可核对的公开资料正文。",
                  allowed_departments=[], kind="official_summary")


def fixture_entry():
    def row(identifier, general=False):
        return {"source": source(identifier).model_dump(), "metadata": {
            "chunk_id": identifier, "source_id": "DOC-" + identifier,
            "document_id": "DOC-" + identifier,
            "category": "health_general" if general else "hospital_department",
            "hospital_id": None if general else "H1"}, "kept": True}
    public, junk = row("PUBLIC", True), row("JUNK", True)
    endo, eye = row("ENDO"), row("EYE")
    branches = [
        {"task": {"scope": "general", "query": "干眼科普", "hospital_id": None, "hospital_name": None},
         "candidates": {"dense": [public, junk], "keyword": [public, junk]}},
        {"task": {"scope": "hospital", "query": "眼表疾病科室", "hospital_id": "H1", "hospital_name": "甲医院"},
         "candidates": {"dense": [endo, eye], "keyword": [endo, eye]}},
    ]
    entry = {"case": {"id": "M", "group": "combined", "query": "流泪与干眼关系，甲医院看哪科？",
                      "gold_groups": [["DOC-PUBLIC"], ["DOC-EYE"]], "hospital_id": "H1",
                      "review_points": ["人工审核点，不得交给重排模型"]},
             "strategy": "planned", "branches": branches,
             "retrieval": {"planned": {"rows": [{"source": source(key).model_dump()}
                                                  for key in ["PUBLIC", "ENDO", "JUNK"]]}}}
    metadata = {item["metadata"]["document_id"]: item["metadata"]
                for item in [public, junk, endo, eye]}
    return entry, metadata


class FakeClient:
    def __init__(self):
        self.inputs = []

    async def structured(self, schema, prompt, payload):
        self.inputs.append((prompt, payload))
        return schema(grades=[{"source_id": row["source_id"],
                               "relevance": 3 if row["source_id"] in {"PUBLIC", "EYE"} else 0,
                               "quote": row["text"][:6] if row["source_id"] in {"PUBLIC", "EYE"} else ""}
                              for row in payload["candidates"]])


def test_frozen_replay_measures_department_evidence_not_just_hospital_name():
    entry, metadata = fixture_entry()
    client = FakeClient()
    result = asyncio.run(replay_case(client, entry, 3, metadata))
    old, new = [result["retrieval"][mode] for mode in ("planned_rrf", "planned_rerank")]
    assert old["metrics"]["hospital_first_match"] == new["metrics"]["hospital_first_match"] == 1
    assert old["metrics"]["hospital_group_recall"] == 0
    assert new["metrics"]["hospital_group_recall"] == 1
    assert old["metrics"]["group_recall"] == 0.5
    assert new["metrics"]["group_recall"] == 1
    assert [row["source"]["id"] for row in new["rows"]] == ["PUBLIC", "EYE"]
    assert all(set(payload) == {"question", "task", "candidates"} for _, payload in client.inputs)
    assert "人工审核点" not in json.dumps(client.inputs, ensure_ascii=False)
    assert "DOC-EYE" not in json.dumps(client.inputs, ensure_ascii=False)


def test_fallback_is_reported_and_does_not_inflate_metrics():
    class Failed:
        async def structured(self, *args):
            raise ProviderError("provider_http", "模拟上游异常")
    entry, metadata = fixture_entry()
    result = asyncio.run(replay_case(Failed(), entry, 3, metadata))
    metrics = summary([result])
    assert metrics["rerank_fallbacks"] == 2
    assert metrics["planned_rerank"]["group_recall"]["mean"] == 0.5


@pytest.mark.parametrize("change", ["ranking", "scope", "already_reranked", "failed"])
def test_changed_or_unreplayable_inputs_are_rejected(change):
    entry, _ = fixture_entry()
    if change == "ranking":
        entry["retrieval"]["planned"]["rows"].reverse()
    elif change == "scope":
        entry["branches"][1]["candidates"]["dense"][0]["metadata"]["hospital_id"] = "OTHER"
    elif change == "already_reranked":
        entry["branches"][0]["rerank"] = {"enabled": True, "status": "ok"}
    else:
        entry["planned_error_code"] = "provider_http"
    with pytest.raises(ValueError):
        prepare_case(entry, 3)


def test_runner_reuses_frozen_candidates_and_same_answer_prompt(tmp_path, monkeypatch):
    import app.config
    import app.providers

    entry, metadata = fixture_entry()
    client = FakeClient()
    original_structured = client.structured
    answer_calls = []

    async def structured(schema, prompt, payload):
        if "sources" not in payload:
            return await original_structured(schema, prompt, payload)
        answer_calls.append((prompt, payload))
        if len(answer_calls) == 1:
            raise ProviderError("invalid_schema", "模拟旧排序回答格式异常")
        return schema(summary="按公开资料说明", abstained=False, claims=[
            {"text": "公开信息", "evidence_id": item["id"], "quote": item["text"][:6]}
            for item in payload["sources"]
        ])
    client.structured = structured
    monkeypatch.setattr(app.config, "Settings", lambda: SimpleNamespace(mode="live", chat_model="fake"))
    monkeypatch.setattr(app.providers, "APIClient", lambda settings: client)
    raw_report = {"complete": True, "corpus_unchanged": True, "top_k": 3,
                  "suite_sha256": "suite", "corpus_sha256": "corpus",
                  "corpus_snapshot": list(metadata.values()), "cases": [copy.deepcopy(entry)]}
    input_path, output = tmp_path / "input.json", tmp_path / "output.json"
    input_path.write_text(json.dumps(raw_report, ensure_ascii=False), encoding="utf-8")
    args = SimpleNamespace(input=input_path, case_ids=None, timeout_seconds=1,
                           with_answers=True, out=output)
    assert asyncio.run(run(args)) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["complete"]
    assert report["summary"]["answer_failures"] == 1
    assert report["summary"]["answers"]["planned_rrf"]["citation_group_coverage"]["mean"] == 0
    assert report["summary"]["answers"]["planned_rerank"]["citation_group_coverage"]["mean"] == 1
    assert len(answer_calls) == 2
    assert all(prompt == PROMPT and set(payload) == {"question", "sources"}
               for prompt, payload in answer_calls)
