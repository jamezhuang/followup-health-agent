from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.evaluate_rag import (
    Case,
    Suite,
    EvalAnswer,
    CatalogLookup,
    answer_metrics,
    answer_payload,
    retrieval_metrics,
    summarize,
    digest,
)


def case(**updates):
    data = {
        "id": "T",
        "group": "test",
        "query": "测试问题",
        "gold_groups": [["A"], ["B"]],
        "review_points": ["人工判断语义"],
    }
    return Case(**{**data, **updates})


def row(document, source_id=None, hospital=None, category="health_general"):
    return {
        "document_id": document,
        "hospital_id": hospital,
        "category": category,
        "source": {"id": source_id or document, "text": "这是可以逐字核对的完整正文。"},
    }


def test_group_recall_and_chunk_rank():
    metrics = retrieval_metrics(case(), [row("X"), row("X"), row("A"), row("A")])
    assert metrics["hit"] is True
    assert metrics["group_recall"] == 0.5
    assert metrics["rr"] == pytest.approx(1 / 3)


def test_hospital_ignores_general_education_publisher():
    rows = [
        row("A", hospital="other"),
        row("B", hospital="target", category="hospital_department"),
        row("C", hospital="other", category="hospital_department"),
    ]
    metrics = retrieval_metrics(case(hospital_id="target"), rows)
    assert metrics["hospital_first_match"] is True
    assert metrics["hospital_precision"] == 0.5


def test_unknown_citation_and_wrong_source_quote_fail():
    answer = EvalAnswer(
        summary="答案",
        abstained=False,
        claims=[
            {"text": "结论", "evidence_id": "made-up", "quote": "可以逐字核对"},
            {"text": "结论", "evidence_id": "chunk-A", "quote": "不存在的引文"},
        ],
    )
    result = answer_metrics(case(), answer, [row("A", "chunk-A")])
    assert result["citation_id_rate"] == 0.5
    assert result["quote_match_rate"] == 0
    assert result["evidence_structure_pass"] is False


def test_valid_quotes_cover_original_document_groups():
    answer = EvalAnswer(
        summary="答案",
        abstained=False,
        claims=[
            {"text": "结论", "evidence_id": "chunk-A", "quote": "可以逐字核对"},
        ],
    )
    result = answer_metrics(case(), answer, [row("A", "chunk-A")])
    assert result["citation_group_coverage"] == 0.5
    assert result["evidence_structure_pass"] is True
    assert result["semantic_support_review"] == "pending"


def test_unsupported_abstention_and_no_gold_in_prompt():
    test_case = case(gold_groups=[])
    answer = EvalAnswer(summary="资料不足", abstained=True, claims=[])
    assert answer_metrics(test_case, answer, [row("X")])["abstention_match"] is True
    assert retrieval_metrics(test_case, [row("X")])["hit"] is None
    assert set(answer_payload(test_case, [row("X")])) == {"question", "sources"}


def test_failed_retrieval_counts_in_denominator():
    entries = [
        {
            "retrieval": {
                mode: {"metrics": retrieval_metrics(case(), [])}
                for mode in ("dense", "keyword", "hybrid")
            },
            "error_code": "timeout",
        },
        {
            "retrieval": {
                mode: {"metrics": retrieval_metrics(case(), [row("A"), row("B")])}
                for mode in ("dense", "keyword", "hybrid")
            }
        },
    ]
    summary = summarize(entries)
    assert summary["hybrid"]["hit"] == {"mean": 0.5, "n": 2}
    assert summary["retrieval_failures"] == 1


def test_suite_validation_and_committed_case_count():
    path = Path(__file__).resolve().parents[1] / "data/eval/gd_eval_v1.json"
    suite = Suite.model_validate_json(path.read_bytes())
    assert len(suite.cases) == 30
    with pytest.raises(ValidationError):
        Suite(version="test", split="test", note="test", cases=[case(), case()])
    with pytest.raises(ValidationError):
        case(gold_groups=[[]])


class FakeEngine:
    def __init__(self, rows):
        self.rows = rows

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement, parameters=None):
        self.parameters = parameters
        return self

    def mappings(self):
        return self

    def all(self):
        return self.rows


def test_chunk_resolves_to_stable_document_and_collision_rejected():
    from types import SimpleNamespace

    chunk = "a" * 64
    saved = {
        "chunk_id": chunk,
        "source_id": "source-A",
        "document_id": "doc-A",
        "category": "hospital_department",
        "hospital_id": "target",
    }
    engine = FakeEngine([saved])
    source = SimpleNamespace(
        id="KB-" + chunk[:24],
        model_dump=lambda: {"id": "KB-" + chunk[:24], "text": "正文"},
    )
    assert CatalogLookup(engine).resolve([source])[0]["document_id"] == "doc-A"
    assert engine.parameters == {"prefixes": [chunk[:24]], "direct": []}
    engine.rows = [saved, {**saved, "chunk_id": "a" * 24 + "b" * 40}]
    with pytest.raises(ValueError, match="唯一关联"):
        CatalogLookup(engine).resolve([source])


def test_stale_metadata_rejected_before_scoring():
    engine = FakeEngine(
        [{"source": {"id": "A", "text": "new"}, "source_revision": "old"}]
    )
    with pytest.raises(ValueError, match="不同步"):
        CatalogLookup(engine).snapshot()


def test_storage_score_is_not_part_of_source_fingerprint():
    source = {"id": "A", "text": "正文"}
    engine = FakeEngine(
        [{"source": {**source, "score": 0}, "source_revision": digest(source)}]
    )
    assert CatalogLookup(engine).snapshot()[0]["source_revision"] == digest(source)


def test_both_batches_compile_and_all_gold_ids_exist(tmp_path):
    import shutil
    from app.knowledge_catalog import load_catalog, compile_catalog
    from scripts.seed_catalog import append_batch

    base = Path(__file__).resolve().parents[1]
    manifest = tmp_path / "knowledge_catalog.json"
    raw = tmp_path / "raw"
    shutil.copyfile(base / "data/knowledge_catalog.json", manifest)
    shutil.copytree(base / "data/knowledge_raw", raw)
    for name in ("gd_batch_01.json", "gd_batch_02.json"):
        append_batch(base / "data/seeds" / name, manifest, raw)
    catalog = load_catalog(manifest)
    sources, metadata = compile_catalog(catalog, raw)
    documents = {record["document_id"] for record in metadata["records"]}
    suite = Suite.model_validate_json((base / "data/eval/gd_eval_v1.json").read_bytes())
    assert len(catalog.hospitals) == 2
    assert len(catalog.departments) >= 18
    assert len(sources) >= 34
    assert all(
        doc in documents
        for case in suite.cases
        for group in case.gold_groups
        for doc in group
    )
