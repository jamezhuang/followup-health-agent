import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .schemas import Source


class DocumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    file: str = Field(min_length=1)
    scope: Literal["public"]
    title: str = Field(min_length=1)
    institution: str = Field(min_length=1)
    url: str
    published_at: str | None = None
    collected_at: str
    kind: Literal["official_summary", "external_excerpt"] = "external_excerpt"
    allowed_departments: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        url = urlsplit(value)

        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("来源 URL 必须是完整的 http/https 地址")

        if url.username or url.password:
            raise ValueError("来源 URL 不能包含账号或密码")

        return value

    @field_validator("published_at", "collected_at")
    @classmethod
    def valid_date(cls, value):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式")

        return value


def parse_document(path: Path):
    suffix = path.suffix.lower()

    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8-sig")

        return [
            {
                "suffix": "",
                "label": "",
                "text": text,
                "location": {"format": suffix[1:]},
                "blocks": [],
            }
        ]

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)

        if reader.is_encrypted:
            raise ValueError(f"{path.name} 是加密 PDF，请先提供解密后的公开文件")

        units = []

        for number, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()

            if not text:
                raise ValueError(
                    f"{path.name} 第 {number} 页没有可提取文字，"
                    "可能是空白页或扫描页；本阶段不静默跳过，"
                    "请先处理后重试"
                )

            units.append(
                {
                    "suffix": f"-P{number:04d}",
                    "label": f"（PDF第{number}页）",
                    "text": text,
                    "location": {
                        "format": "pdf",
                        "page": number,
                    },
                    "blocks": [],
                }
            )

        return units

    if suffix == ".docx":
        from docx import Document
        from docx.text.paragraph import Paragraph

        with ZipFile(path) as archive:
            expanded_size = sum(item.file_size for item in archive.infolist())

            if expanded_size > 100 * 1024 * 1024:
                raise ValueError("DOCX 解压后超过 100 MiB，请拆分文件")

        parts = []
        blocks = []
        offset = 0
        paragraph_number = 0
        table_number = 0

        def append(text, location):
            nonlocal offset

            text = text.strip()

            if not text:
                return

            if parts:
                offset += 2

            blocks.append(
                {
                    **location,
                    "start": offset,
                    "end": offset + len(text),
                }
            )

            parts.append(text)
            offset += len(text)

        for item in Document(path).iter_inner_content():
            if isinstance(item, Paragraph):
                paragraph_number += 1

                append(
                    item.text,
                    {
                        "type": "paragraph",
                        "paragraph": paragraph_number,
                    },
                )
            else:
                table_number += 1

                for row_number, row in enumerate(item.rows, 1):
                    append(
                        " | ".join(cell.text for cell in row.cells),
                        {
                            "type": "table_row",
                            "table": table_number,
                            "row": row_number,
                        },
                    )

        return [
            {
                "suffix": "",
                "label": "",
                "text": "\n\n".join(parts),
                "location": {
                    "format": "docx",
                    "area": "body",
                },
                "blocks": blocks,
            }
        ]

    raise ValueError(f"不支持 {suffix}，本阶段支持 txt/md/pdf/docx")


def prepare(manifest: Path, raw_dir: Path):
    entries = json.loads(manifest.read_text(encoding="utf-8-sig"))

    if not isinstance(entries, list) or not entries:
        raise ValueError("来源清单必须是非空 JSON 数组")

    specs = [DocumentSpec.model_validate(entry) for entry in entries]

    if len({spec.id for spec in specs}) != len(specs):
        raise ValueError("来源清单中的 id 不能重复")

    root = raw_dir.resolve()
    sources = []
    records = []
    seen = set()

    for spec in specs:
        relative = Path(spec.file)
        path = (root / relative).resolve()

        if relative.is_absolute() or not path.is_relative_to(root):
            raise ValueError(f"{spec.id}: 文件必须位于 knowledge_raw 目录内")

        if not path.is_file():
            raise ValueError(f"{spec.id}: 找不到文件 {spec.file}")

        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError(f"{spec.id}: 文件超过 20 MiB，请拆分后导入")

        digest = hashlib.sha256(path.read_bytes()).hexdigest()

        units = parse_document(path)

        if not units:
            raise ValueError(f"{spec.id}: 文档没有可导入正文")

        for unit in units:
            if not unit["text"].strip():
                raise ValueError(f"{spec.id}: 文档正文为空")

            source_id = spec.id + unit["suffix"]

            if source_id in seen:
                raise ValueError(f"解析后的来源 ID 冲突: {source_id}")

            seen.add(source_id)

            payload = spec.model_dump(exclude={"file", "scope"})

            payload.update(
                id=source_id,
                title=spec.title + unit["label"],
                text=unit["text"],
            )

            source = Source.model_validate(payload)

            sources.append(source.model_dump(exclude={"score"}))

            records.append(
                {
                    "source_id": source_id,
                    "document_id": spec.id,
                    "file": spec.file,
                    "file_sha256": digest,
                    "parser_version": 1,
                    "location": unit["location"],
                    "blocks": unit["blocks"],
                }
            )

    return sources, records
