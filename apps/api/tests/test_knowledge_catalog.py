import copy
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import create_engine, event

from app.catalog_store import CatalogStore
from app.knowledge_catalog import (
    Catalog,
    compile_catalog,
)
from app.schemas import Source


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

        (self.root / "sample.txt").write_text(
            "模拟公开资料，包含中文。",
            encoding="utf-8",
        )

        self.data = {
            "hospitals": [
                {
                    "id": "H1",
                    "name": "测试医院",
                    "province": "广东省",
                    "city": "广州市",
                    "url": "https://example.org/",
                    "verified_at": "2026-10-06",
                },
                {
                    "id": "H2",
                    "name": "另一测试医院",
                    "province": "广东省",
                    "city": "深圳市",
                    "url": ("https://example.org/other"),
                    "verified_at": "2026-10-06",
                },
            ],
            "departments": [
                {
                    "id": "D1",
                    "name": "耳专科",
                    "standard_name": "耳鼻咽喉科",
                    "hospital_id": "H1",
                    "url": ("https://example.org/ear"),
                    "verified_at": "2026-10-06",
                },
            ],
            "items": [
                {
                    "id": "S1",
                    "file": "sample.txt",
                    "scope": "public",
                    "title": "测试公开资料",
                    "institution": "测试医院",
                    "url": ("https://example.org/ear"),
                    "collected_at": "2026-10-06",
                    "category": ("hospital_department"),
                    "hospital_id": "H1",
                    "department_id": "D1",
                    "status": "published",
                    "review_status": ("source_checked"),
                    "verified_at": "2026-10-06",
                    "review_note": ("仅用于模拟来源校验"),
                },
            ],
        }

    def tearDown(self):
        self.directory.cleanup()

    def test_compile_compatible_and_repeatable(self):
        catalog = Catalog.model_validate(self.data)

        sources, metadata = compile_catalog(
            catalog,
            self.root,
        )

        self.assertEqual(
            (sources, metadata),
            compile_catalog(
                catalog,
                self.root,
            ),
        )

        source = Source.model_validate(sources[0])

        self.assertIn("中文", source.text)
        self.assertNotIn(
            "category",
            sources[0],
        )

        self.assertEqual(
            metadata["records"][0]["knowledge"]["category"],
            "hospital_department",
        )

    def test_private_scope_rejected(self):
        self.data["items"][0]["scope"] = "private"

        with self.assertRaises(ValidationError):
            Catalog.model_validate(self.data)

    def test_unknown_category_rejected(self):
        self.data["items"][0]["category"] = "anything"

        with self.assertRaises(ValidationError):
            Catalog.model_validate(self.data)

    def test_cross_hospital_department_rejected(self):
        self.data["items"][0].update(
            hospital_id="H2",
            institution="另一测试医院",
        )

        with self.assertRaises(ValidationError):
            Catalog.model_validate(self.data)

    def test_cross_hospital_campus_rejected(self):
        self.data["campuses"] = [
            {
                "id": "C1",
                "name": "测试院区",
                "hospital_id": "H2",
                "address": "模拟地址",
                "url": ("https://example.org/campus"),
                "verified_at": "2026-10-06",
            },
        ]

        self.data["departments"][0]["campus_id"] = "C1"

        with self.assertRaises(ValidationError):
            Catalog.model_validate(self.data)

    def test_publication_and_triage_review_required(self):
        self.data["items"][0]["review_status"] = "unreviewed"

        with self.assertRaises(ValidationError):
            Catalog.model_validate(self.data)

        self.data["items"][0].update(
            category="symptom_triage",
            review_status="source_checked",
        )

        with self.assertRaises(ValidationError):
            Catalog.model_validate(self.data)

    def test_draft_not_exported(self):
        draft = copy.deepcopy(self.data["items"][0])

        draft.update(
            id="S2",
            file="not-created.txt",
            status="draft",
            review_status="unreviewed",
        )

        self.data["items"].append(draft)

        sources, _ = compile_catalog(
            Catalog.model_validate(self.data),
            self.root,
        )

        self.assertEqual(
            [source["id"] for source in sources],
            ["S1"],
        )

    def test_duplicate_id_rejected(self):
        self.data["items"].append(copy.deepcopy(self.data["items"][0]))

        with self.assertRaises(ValidationError):
            Catalog.model_validate(self.data)

    def test_path_escape_rejected(self):
        self.data["items"][0]["file"] = "../outside.txt"

        with self.assertRaises(ValueError):
            compile_catalog(
                Catalog.model_validate(self.data),
                self.root,
            )

    def store(self):
        engine = create_engine("sqlite://")

        @event.listens_for(
            engine,
            "connect",
        )
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        return CatalogStore(engine=engine)

    def test_database_idempotency_and_city_query(self):
        catalog = Catalog.model_validate(self.data)

        sources, metadata = compile_catalog(
            catalog,
            self.root,
        )

        store = self.store()

        try:
            first = store.sync(
                catalog,
                sources,
                metadata,
            )
            second = store.sync(
                catalog,
                sources,
                metadata,
            )

            self.assertEqual(
                first["updated"],
                4,
            )
            self.assertEqual(
                second["updated"],
                0,
            )
            self.assertEqual(
                store.status()["kb_catalog_versions"],
                1,
            )

            result = store.departments_in_city(
                "广州市",
                "耳鼻咽喉科",
            )

            self.assertEqual(
                result[0]["hospital"],
                "测试医院",
            )

            self.assertEqual(
                store.departments_in_city("深圳市"),
                [],
            )

        finally:
            store.close()

    def test_failed_sync_rolls_back(self):
        catalog = Catalog.model_validate(self.data)

        sources, metadata = compile_catalog(
            catalog,
            self.root,
        )

        store = self.store()

        try:
            store.sync(
                catalog,
                sources,
                metadata,
            )

            # 故意构造错误对象，
            # 检查失败时是否完整回滚。
            catalog.hospitals[0].name = "不应保存的名称"
            catalog.departments[0].hospital_id = "H2"

            with self.assertRaises(ValueError):
                store.sync(
                    catalog,
                    sources,
                    metadata,
                )

            result = store.departments_in_city("广州市")

            self.assertEqual(
                result[0]["hospital"],
                "测试医院",
            )

            self.assertEqual(
                store.status()["kb_catalog_versions"],
                1,
            )

        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
