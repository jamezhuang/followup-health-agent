import hashlib
import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .document_ingest import DocumentSpec, prepare

Category = Literal[
    "health_general",
    "symptom_triage",
    "hospital_department",
    "hospital_service",
    "health_reference",
]


def fingerprint(value):
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Entity(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    name: str = Field(
        min_length=1,
        max_length=120,
    )
    url: str
    verified_at: str

    @field_validator("url")
    @classmethod
    def check_url(cls, value):
        parsed = urlsplit(value)

        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("必须提供完整的官方来源 URL")

        if parsed.username or parsed.password:
            raise ValueError("URL 不能包含账号或密码")

        return value

    @field_validator("verified_at")
    @classmethod
    def check_date(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("核验日期必须是 YYYY-MM-DD")

        return value


class Hospital(Entity):
    province: str = Field(min_length=1)
    city: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class Campus(Entity):
    hospital_id: str
    address: str = Field(min_length=1)


class Department(Entity):
    hospital_id: str
    campus_id: str | None = None
    standard_name: str = Field(min_length=1)

    aliases: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class KnowledgeItem(DocumentSpec):
    category: Category

    topics: list[str] = Field(default_factory=list)

    population: list[
        Literal[
            "general",
            "adult",
            "child",
            "pregnant",
            "older",
        ]
    ] = Field(
        default_factory=lambda: ["general"],
        min_length=1,
    )

    hospital_id: str | None = None
    campus_id: str | None = None
    department_id: str | None = None

    status: Literal[
        "draft",
        "published",
    ] = "draft"

    review_status: Literal[
        "unreviewed",
        "source_checked",
        "clinical_reviewed",
    ] = "unreviewed"

    verified_at: str | None = None
    review_note: str = ""

    @field_validator("verified_at")
    @classmethod
    def check_date(cls, value):
        if value is not None:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("核验日期必须是 YYYY-MM-DD")

        return value

    @model_validator(mode="after")
    def publication_rules(self):
        if (
            self.category
            in {
                "hospital_department",
                "hospital_service",
            }
            and not self.hospital_id
        ):
            raise ValueError("医院类资料必须关联 hospital_id")

        if self.status == "published":
            if (
                self.review_status == "unreviewed"
                or not self.verified_at
                or not self.review_note.strip()
            ):
                raise ValueError("发布资料必须填写审核状态、" "核验日期和审核说明")

            if (
                self.category == "symptom_triage"
                and self.review_status != "clinical_reviewed"
            ):
                raise ValueError("症状分流资料必须经过" "临床审核才能发布")

        return self


class Catalog(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    schema_version: Literal[1] = 1

    hospitals: list[Hospital] = Field(default_factory=list)
    campuses: list[Campus] = Field(default_factory=list)
    departments: list[Department] = Field(default_factory=list)

    items: list[KnowledgeItem] = Field(min_length=1)

    @model_validator(mode="after")
    def check_links(self):
        def unique(values, label):
            if len({value.id for value in values}) != len(values):
                raise ValueError(f"{label} 的 id 不能重复")

            return {value.id: value for value in values}

        hospitals = unique(
            self.hospitals,
            "医院",
        )
        campuses = unique(
            self.campuses,
            "院区",
        )
        departments = unique(
            self.departments,
            "科室",
        )
        unique(self.items, "知识条目")

        for obj in [
            *self.campuses,
            *self.departments,
            *self.items,
        ]:
            hospital_id = obj.hospital_id

            if hospital_id is not None and hospital_id not in hospitals:
                raise ValueError(f"{obj.id}: hospital_id 不存在")

            campus_id = getattr(
                obj,
                "campus_id",
                None,
            )

            if campus_id is not None:
                if (
                    campus_id not in campuses
                    or campuses[campus_id].hospital_id != hospital_id
                ):
                    raise ValueError(f"{obj.id}: 院区不存在" "或不属于关联医院")

        for item in self.items:
            if item.department_id is not None:
                department = departments.get(item.department_id)

                if department is None or department.hospital_id != item.hospital_id:
                    raise ValueError(f"{item.id}: 科室不存在" "或不属于关联医院")

                if (
                    department.campus_id is not None
                    and item.campus_id != department.campus_id
                ):
                    raise ValueError(f"{item.id}: 院区与科室不一致")

            if item.hospital_id is not None:
                hospital = hospitals[item.hospital_id]

                if item.institution not in [
                    hospital.name,
                    *hospital.aliases,
                ]:
                    raise ValueError(f"{item.id}: institution" " 与关联医院不一致")

        return self


def load_catalog(path):
    content = Path(path).read_text(encoding="utf-8-sig")
    return Catalog.model_validate(json.loads(content))


def compile_catalog(catalog, raw_dir):
    published = [item for item in catalog.items if item.status == "published"]

    if not published:
        raise ValueError("没有已发布条目；" "草稿可以 validate，" "但不能生成入库包")

    # 只把旧解析器认识的字段传给它，
    # 避免 Source 出现 extra_forbidden。
    fields = set(DocumentSpec.model_fields)

    entries = [
        {key: value for key, value in item.model_dump().items() if key in fields}
        for item in published
    ]

    with tempfile.TemporaryDirectory() as temporary:
        manifest = Path(temporary) / "manifest.json"

        manifest.write_text(
            json.dumps(
                entries,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        sources, records = prepare(
            manifest,
            Path(raw_dir),
        )

    items = {item.id: item for item in published}

    sources_by_id = {source["id"]: source for source in sources}

    for record in records:
        record["knowledge"] = items[record["document_id"]].model_dump()

        record["source_revision"] = fingerprint(sources_by_id[record["source_id"]])

    metadata = {
        "schema_version": 1,
        "catalog_revision": fingerprint(catalog.model_dump()),
        "catalog": catalog.model_dump(),
        "records": records,
    }

    return sources, metadata
