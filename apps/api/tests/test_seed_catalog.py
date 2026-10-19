import json
import shutil
from pathlib import Path

import pytest

from app.knowledge_catalog import (
    compile_catalog,
    load_catalog,
)
from app.schemas import Source
from scripts.seed_catalog import append_batch

BASE = Path(__file__).resolve().parents[1]


@pytest.fixture
def corpus(tmp_path):
    manifest = tmp_path / "knowledge_catalog.json"
    raw = tmp_path / "knowledge_raw"
    batch = tmp_path / "batch.json"

    shutil.copy(
        BASE / "data/knowledge_catalog.json",
        manifest,
    )

    shutil.copytree(
        BASE / "data/knowledge_raw",
        raw,
    )

    shutil.copy(
        BASE / "data/seeds/gd_batch_01.json",
        batch,
    )

    return batch, manifest, raw


def test_dry_run_does_not_write(corpus):
    batch, manifest, raw = corpus

    before = manifest.read_bytes()
    names = set(raw.iterdir())

    result = append_batch(
        batch,
        manifest,
        raw,
        dry_run=True,
    )

    assert result["new_items"] >= 0
    assert manifest.read_bytes() == before
    assert set(raw.iterdir()) == names
    assert not list(manifest.parent.glob("*.bak-*.json"))


def test_repeat_is_idempotent_and_old_items_remain(
    corpus,
):
    batch, manifest, raw = corpus
    previous = load_catalog(manifest)

    append_batch(batch, manifest, raw)
    after = manifest.read_bytes()

    result = append_batch(
        batch,
        manifest,
        raw,
    )

    assert result["new_items"] == result["new_departments"] == result["new_files"] == 0

    assert manifest.read_bytes() == after

    current = {v.id: v for v in load_catalog(manifest).items}

    assert all(current[v.id] == v for v in previous.items)


def test_conflict_fails_before_any_write(corpus):
    batch, manifest, raw = corpus

    target = raw / "seed_general-diabetes-01.txt"
    target.write_text(
        "用户已有正文，不能覆盖。",
        encoding="utf-8",
    )

    before = manifest.read_bytes()
    names = set(raw.iterdir())

    with pytest.raises(
        ValueError,
        match="正文内容冲突",
    ):
        append_batch(batch, manifest, raw)

    assert manifest.read_bytes() == before
    assert set(raw.iterdir()) == names

    assert target.read_text(encoding="utf-8") == "用户已有正文，不能覆盖。"


def test_compiled_sources_and_links(corpus):
    batch, manifest, raw = corpus
    append_batch(batch, manifest, raw)

    catalog = load_catalog(manifest)
    sources, metadata = compile_catalog(
        catalog,
        raw,
    )

    assert len(sources) == len([v for v in catalog.items if v.status == "published"])

    assert all(Source.model_validate(v).text.strip() for v in sources)

    department_ids = {v.id for v in catalog.departments}

    assert all(
        v.department_id in department_ids for v in catalog.items if v.department_id
    )

    assert "细菌" in next(
        v["text"] for v in sources if v["id"] == "GENERAL-ANTIBIOTIC-01"
    )

    assert all("category" not in v for v in sources)

    assert metadata["records"]


def test_unknown_hospital_fails_without_writes(
    corpus,
):
    batch, manifest, raw = corpus

    data = json.loads(batch.read_text(encoding="utf-8"))

    data["departments"][0]["hospital_id"] = "UNKNOWN"

    batch.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    before = manifest.read_bytes()
    names = set(raw.iterdir())

    with pytest.raises(
        ValueError,
        match="医院不存在",
    ):
        append_batch(batch, manifest, raw)

    assert manifest.read_bytes() == before
    assert set(raw.iterdir()) == names


def test_seed_cannot_publish_triage(corpus):
    batch, manifest, raw = corpus

    data = json.loads(batch.read_text(encoding="utf-8"))

    data["items"][0]["category"] = "symptom_triage"

    batch.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        append_batch(batch, manifest, raw)
