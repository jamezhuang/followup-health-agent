import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.knowledge_catalog import (
    Catalog,
    Department,
    KnowledgeItem,
    load_catalog,
)
from scripts.prepare_knowledge import atomic_write

BASE = Path(__file__).resolve().parents[1]


class SeedDepartment(Department):
    text: str = Field(min_length=20, max_length=5000)


class SeedItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    title: str = Field(min_length=1)
    institution: str = Field(min_length=1)
    url: str
    published_at: str | None = None

    category: Literal[
        "health_general",
        "hospital_service",
    ]

    hospital_id: str | None = None
    topics: list[str] = Field(min_length=1)

    population: list[
        Literal[
            "general",
            "adult",
            "child",
            "pregnant",
            "older",
        ]
    ] = Field(default_factory=lambda: ["general"])

    text: str = Field(
        min_length=20,
        max_length=5000,
    )


class Batch(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    schema_version: Literal[1]
    verified_at: str
    review_note: str = Field(min_length=10)
    departments: list[SeedDepartment]
    items: list[SeedItem]


def encoded(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def append_batch(
    batch_path,
    manifest_path,
    raw_dir,
    dry_run=False,
):
    manifest_path = Path(manifest_path)
    raw_dir = Path(raw_dir).resolve()

    original = manifest_path.read_bytes()
    catalog = load_catalog(manifest_path)

    batch = Batch.model_validate(
        json.loads(Path(batch_path).read_text(encoding="utf-8-sig"))
    )

    if not batch.departments and not batch.items:
        raise ValueError("批次不能为空")

    merged = catalog.model_dump()
    hospitals = {h.id: h for h in catalog.hospitals}

    additions = []

    def item(values, text):
        record = KnowledgeItem.model_validate(
            {
                "scope": "public",
                "kind": "official_summary",
                "collected_at": batch.verified_at,
                "verified_at": batch.verified_at,
                "published_at": None,
                "status": "published",
                "review_status": "source_checked",
                "review_note": batch.review_note,
                **values,
            }
        )

        additions.append(
            (
                record.model_dump(),
                text.strip() + "\n",
            )
        )

    def merge(values, incoming, label):
        current = {v["id"]: v for v in values}
        added = 0

        for value in incoming:
            previous = current.get(value["id"])

            if previous is not None:
                if previous != value:
                    raise ValueError(
                        f"{label}同 ID 内容冲突：" f"{value['id']}，请人工核对"
                    )
                continue

            values.append(value)
            current[value["id"]] = value
            added += 1

        return added

    departments = []

    for entry in batch.departments:
        hospital = hospitals.get(entry.hospital_id)

        if hospital is None:
            raise ValueError(f"医院不存在：{entry.hospital_id}，" "请先完成第 2 步")

        departments.append(
            Department.model_validate(entry.model_dump(exclude={"text"})).model_dump()
        )

        item(
            {
                "id": entry.id + "-KB",
                "file": ("seed_" + entry.id.lower() + ".txt"),
                "title": (hospital.name + entry.name + "服务范围（官方摘要）"),
                "institution": hospital.name,
                "url": entry.url,
                "category": "hospital_department",
                "topics": entry.topics,
                "hospital_id": entry.hospital_id,
                "campus_id": entry.campus_id,
                "department_id": entry.id,
                "allowed_departments": [entry.name],
            },
            entry.text,
        )

    for entry in batch.items:
        values = entry.model_dump(exclude={"text"})

        values["file"] = "seed_" + entry.id.lower() + ".txt"

        item(values, entry.text)

    if len({v["id"] for v in departments}) != len(departments):
        raise ValueError("批次中的科室 ID 不能重复")

    if len({v["id"] for v, _ in additions}) != len(additions):
        raise ValueError("批次中的知识 ID 不能重复")

    added_departments = merge(
        merged["departments"],
        departments,
        "科室",
    )

    added_items = merge(
        merged["items"],
        [v for v, _ in additions],
        "知识",
    )

    result = Catalog.model_validate(merged)

    # 检查完整批次后再写入，不覆盖冲突正文。
    pending = {}

    owners = {v.file.casefold(): v.id for v in catalog.items}

    for record, text in additions:
        owner = owners.get(record["file"].casefold())

        if owner is not None and owner != record["id"]:
            raise ValueError("正文文件已被其他条目使用：" + record["file"])

        target = (raw_dir / record["file"]).resolve()

        if target.parent != raw_dir:
            raise ValueError("正文文件不能位于原始资料目录之外")

        content = text.encode("utf-8")

        if target in pending and pending[target] != content:
            raise ValueError(f"批次中正文冲突：{record['id']}")

        pending[target] = content

        if target.exists() and target.read_bytes() != content:
            raise ValueError(f"正文内容冲突：{target.name}，" "请人工核对")

    missing = {path: content for path, content in pending.items() if not path.exists()}

    summary = {
        "new_departments": added_departments,
        "new_items": added_items,
        "new_files": len(missing),
        "total_items": len(result.items),
    }

    if dry_run:
        return summary

    if added_departments or added_items:
        digest = hashlib.sha256(original).hexdigest()[:12]

        backup = manifest_path.with_name(
            manifest_path.stem + ".bak-" + digest + ".json"
        )

        if backup.exists() and backup.read_bytes() != original:
            raise ValueError("备份文件内容冲突")

        if not backup.exists():
            atomic_write(backup, original)

    for target, content in missing.items():
        atomic_write(target, content)

    if added_departments or added_items:
        atomic_write(
            manifest_path,
            encoded(result.model_dump()),
        )

    return summary


def main():
    parser = argparse.ArgumentParser(
        description=("追加已核验的公开资料批次，" "不调用 API")
    )

    parser.add_argument(
        "--batch",
        type=Path,
        default=(BASE / "data/seeds/gd_batch_01.json"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=(BASE / "data/knowledge_catalog.json"),
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=(BASE / "data/knowledge_raw"),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    args = parser.parse_args()

    print(
        append_batch(
            args.batch,
            args.manifest,
            args.raw_dir,
            args.dry_run,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(f"追加失败：{error}") from None
