import os

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .knowledge_catalog import fingerprint


class CatalogStore:
    def __init__(self, url=None, engine=None):
        if engine is None:
            url = url or os.getenv(
                "RAG_DATABASE_URL",
                "",
            )

            if not url.startswith("postgresql+psycopg://"):
                raise ValueError("RAG_DATABASE_URL 必须使用 " "postgresql+psycopg")

            engine = create_engine(
                url,
                pool_pre_ping=True,
                hide_parameters=True,
                connect_args={
                    "connect_timeout": 5,
                },
            )

        self.engine = engine
        self.metadata = MetaData()

        data_type = JSON().with_variant(
            JSONB(),
            "postgresql",
        )

        def table(name, *columns):
            return Table(
                name,
                self.metadata,
                Column(
                    "id",
                    String,
                    primary_key=True,
                ),
                *columns,
                Column(
                    "revision",
                    String(64),
                    nullable=False,
                ),
                Column(
                    "payload",
                    data_type,
                    nullable=False,
                ),
            )

        self.hospitals = table(
            "kb_hospitals",
            Column(
                "name",
                String,
                nullable=False,
            ),
            Column(
                "province",
                String,
                nullable=False,
                index=True,
            ),
            Column(
                "city",
                String,
                nullable=False,
                index=True,
            ),
        )

        self.campuses = table(
            "kb_campuses",
            Column(
                "hospital_id",
                String,
                ForeignKey("kb_hospitals.id"),
                nullable=False,
                index=True,
            ),
            Column(
                "name",
                String,
                nullable=False,
            ),
        )

        self.departments = table(
            "kb_departments",
            Column(
                "hospital_id",
                String,
                ForeignKey("kb_hospitals.id"),
                nullable=False,
                index=True,
            ),
            Column(
                "campus_id",
                String,
                ForeignKey("kb_campuses.id"),
            ),
            Column(
                "name",
                String,
                nullable=False,
            ),
            Column(
                "standard_name",
                String,
                nullable=False,
                index=True,
            ),
        )

        self.sources = table(
            "kb_source_metadata",
            Column(
                "document_id",
                String,
                nullable=False,
                index=True,
            ),
            Column(
                "category",
                String,
                nullable=False,
                index=True,
            ),
            Column(
                "hospital_id",
                String,
                ForeignKey("kb_hospitals.id"),
                index=True,
            ),
            Column(
                "campus_id",
                String,
                ForeignKey("kb_campuses.id"),
            ),
            Column(
                "department_id",
                String,
                ForeignKey("kb_departments.id"),
            ),
            Column(
                "source_revision",
                String(64),
                nullable=False,
            ),
        )

        self.versions = Table(
            "kb_catalog_versions",
            self.metadata,
            Column(
                "id",
                String(64),
                primary_key=True,
            ),
            Column(
                "payload",
                data_type,
                nullable=False,
            ),
            Column(
                "created_at",
                DateTime(timezone=True),
                server_default=func.now(),
                nullable=False,
            ),
        )

    def lock(self, connection):
        if self.engine.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config(" "'statement_timeout', " "'15000', true)")
            )

            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext("
                    "'carebrief_catalog_sync'"
                    "))"
                )
            )

    def initialize(self):
        with self.engine.begin() as connection:
            self.lock(connection)
            self.metadata.create_all(connection)

    def sync(self, catalog, sources, metadata):
        self.initialize()

        updated = 0
        skipped = 0

        with self.engine.begin() as connection:
            self.lock(connection)

            # 核对正文已经进入现有 RAG 数据库，
            # 防止新元数据绑定旧正文。
            if self.engine.dialect.name == "postgresql":
                for source in sources:
                    saved = connection.execute(
                        text(
                            "SELECT source "
                            "FROM rag_documents "
                            "WHERE source_id=:id "
                            "AND active=true"
                        ),
                        {
                            "id": source["id"],
                        },
                    ).scalar_one_or_none()

                    if not isinstance(saved, dict) or any(
                        saved.get(key) != value for key, value in source.items()
                    ):
                        raise ValueError(
                            f"{source['id']}: "
                            "请先执行 scripts.rag import "
                            "导入对应正文"
                        )

            def save(table, payload, values):
                nonlocal updated, skipped

                revision = fingerprint(payload)

                old = (
                    connection.execute(select(table).where(table.c.id == values["id"]))
                    .mappings()
                    .first()
                )

                if (
                    old
                    and "hospital_id" in values
                    and old["hospital_id"] != values["hospital_id"]
                ):
                    raise ValueError("已有记录不能换绑医院，" "请使用新的稳定 ID")

                if old and old["revision"] == revision:
                    skipped += 1
                    return

                values = {
                    **values,
                    "revision": revision,
                    "payload": payload,
                }

                if old:
                    connection.execute(
                        table.update()
                        .where(table.c.id == values["id"])
                        .values(**values)
                    )
                else:
                    connection.execute(table.insert().values(**values))

                updated += 1

            for obj in catalog.hospitals:
                save(
                    self.hospitals,
                    obj.model_dump(),
                    {
                        key: getattr(obj, key)
                        for key in (
                            "id",
                            "name",
                            "province",
                            "city",
                        )
                    },
                )

            for obj in catalog.campuses:
                save(
                    self.campuses,
                    obj.model_dump(),
                    {
                        key: getattr(obj, key)
                        for key in (
                            "id",
                            "hospital_id",
                            "name",
                        )
                    },
                )

            for obj in catalog.departments:
                save(
                    self.departments,
                    obj.model_dump(),
                    {
                        key: getattr(obj, key)
                        for key in (
                            "id",
                            "hospital_id",
                            "campus_id",
                            "name",
                            "standard_name",
                        )
                    },
                )

            for record in metadata["records"]:
                knowledge = record["knowledge"]

                values = {
                    key: knowledge[key]
                    for key in (
                        "category",
                        "hospital_id",
                        "campus_id",
                        "department_id",
                    )
                }

                values.update(
                    id=record["source_id"],
                    document_id=record["document_id"],
                    source_revision=(record["source_revision"]),
                )

                save(
                    self.sources,
                    record,
                    values,
                )

            revision = fingerprint(metadata)

            existing = connection.scalar(
                select(self.versions.c.id).where(self.versions.c.id == revision)
            )

            if existing is None:
                connection.execute(
                    self.versions.insert().values(
                        id=revision,
                        payload=metadata,
                    )
                )

        return {
            "updated": updated,
            "unchanged": skipped,
            "catalog_version": revision,
        }

    def status(self):
        with self.engine.connect() as connection:
            return {
                table.name: connection.scalar(select(func.count()).select_from(table))
                for table in (
                    self.hospitals,
                    self.campuses,
                    self.departments,
                    self.sources,
                    self.versions,
                )
            }

    def departments_in_city(
        self,
        city,
        standard_name=None,
    ):
        statement = select(
            self.departments.c.payload,
            self.hospitals.c.name.label("hospital"),
        )

        statement = statement.join(
            self.hospitals,
            self.departments.c.hospital_id == self.hospitals.c.id,
        )

        statement = statement.where(self.hospitals.c.city == city)

        if standard_name:
            statement = statement.where(
                self.departments.c.standard_name == standard_name
            )

        with self.engine.connect() as connection:
            return [
                {
                    "hospital": row.hospital,
                    "department": row.payload,
                }
                for row in connection.execute(statement.order_by(self.departments.c.id))
            ]

    def close(self):
        self.engine.dispose()
