import asyncio
import hashlib
import json
import math
import os
from contextlib import contextmanager

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .providers import ProviderError
from .schemas import Source


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_text(content, size=600, overlap=80):
    if size < 100 or not 0 <= overlap < size:
        raise ValueError("分块长度至少100，重叠长度必须小于分块长度。")

    chunks, start = [], 0

    while start < len(content):
        end = min(start + size, len(content))

        if content[start:end].strip():
            chunks.append(
                {
                    "start": start,
                    "end": end,
                    "text": content[start:end],
                }
            )

        if end == len(content):
            break

        start = end - overlap

    return chunks


def check_vectors(vectors, count, dimensions):
    if len(vectors) != count:
        raise ProviderError(
            "embedding_format",
            "嵌入数量不正确，未更新该文档。",
        )

    for vector in vectors:
        if (
            len(vector) != dimensions
            or any(
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or not math.isfinite(x)
                for x in vector
            )
            or sum(x * x for x in vector) == 0
        ):
            raise ProviderError(
                "embedding_format",
                "嵌入维度或内容不正确，未更新该文档。",
            )


class PgKnowledgeSearch:
    """第一阶段仅存公共知识；私人资料使用后续独立的权限检索入口。"""

    def __init__(self, settings, client, database_url=None):
        self.settings, self.client = settings, client

        self.size = int(os.getenv("RAG_CHUNK_SIZE", "600"))
        self.overlap = int(os.getenv("RAG_CHUNK_OVERLAP", "80"))
        self.min_score = float(os.getenv("RAG_MIN_SCORE", "0.2"))

        split_text("", self.size, self.overlap)

        if not 0 <= self.min_score <= 1 or not 1 <= settings.dimensions <= 16000:
            raise ValueError("请检查RAG_MIN_SCORE与EMBEDDING_DIMENSIONS。")

        url = database_url or os.getenv("RAG_DATABASE_URL", "")

        try:
            if make_url(url).drivername != "postgresql+psycopg":
                raise ValueError()
        except (ValueError, SQLAlchemyError):
            raise ProviderError(
                "rag_config",
                "请设置postgresql+psycopg格式的RAG_DATABASE_URL。",
            ) from None

        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"connect_timeout": 5},
        )

        self.profile = digest(
            {
                "schema": 1,
                "model": settings.embedding_model,
                "base": settings.embedding_base,
                "dimensions": settings.dimensions,
            }
        )

        metadata = MetaData()

        self.profiles = Table(
            "rag_profile",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("fingerprint", String(64), nullable=False),
        )

        self.documents = Table(
            "rag_documents",
            metadata,
            Column("revision", String(64), primary_key=True),
            Column("source_id", Text, nullable=False, index=True),
            Column("active", Boolean, nullable=False),
            Column("source", JSONB, nullable=False),
        )

        Index(
            "rag_current_source",
            self.documents.c.source_id,
            unique=True,
            postgresql_where=self.documents.c.active.is_(True),
        )

        self.chunks = Table(
            "rag_chunks",
            metadata,
            Column("id", String(64), primary_key=True),
            Column(
                "revision",
                ForeignKey("rag_documents.revision"),
                nullable=False,
                index=True,
            ),
            Column("start", Integer, nullable=False),
            Column("end", Integer, nullable=False),
            Column("text", Text, nullable=False),
            Column(
                "embedding",
                VECTOR(settings.dimensions),
                nullable=False,
            ),
        )

        self.metadata = metadata

    @contextmanager
    def connection(self):
        try:
            with self.engine.begin() as connection:
                connection.execute(text("SET LOCAL statement_timeout = '30s'"))
                yield connection
        except SQLAlchemyError:
            raise ProviderError(
                "rag_database",
                "RAG数据库操作失败，请检查容器、连接配置或是否已初始化。",
            ) from None

    def check_profile(self, connection):
        stored = connection.scalar(
            select(self.profiles.c.fingerprint).where(self.profiles.c.id == 1)
        )

        if stored != self.profile:
            raise ProviderError(
                "rag_profile",
                "数据库嵌入配置不匹配；请恢复原模型、地址和维度，" "或使用新库重建。",
            )

    def initialize(self):
        with self.connection() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

            self.metadata.create_all(connection)

            connection.execute(
                insert(self.profiles)
                .values(id=1, fingerprint=self.profile)
                .on_conflict_do_nothing()
            )

            self.check_profile(connection)

    def status(self):
        with self.connection() as connection:
            self.check_profile(connection)

            documents = connection.scalar(
                select(func.count())
                .select_from(self.documents)
                .where(self.documents.c.active.is_(True))
            )

            chunks = connection.scalar(
                select(func.count())
                .select_from(self.chunks.join(self.documents))
                .where(self.documents.c.active.is_(True))
            )

            return {
                "documents": documents,
                "chunks": chunks,
                "dimensions": self.settings.dimensions,
            }

    def is_current(self, revision):
        with self.connection() as connection:
            self.check_profile(connection)

            return bool(
                connection.scalar(
                    select(self.documents.c.active).where(
                        self.documents.c.revision == revision
                    )
                )
            )

    def ready(self):
        with self.connection() as connection:
            self.check_profile(connection)

            return (
                connection.scalar(
                    select(self.chunks.c.id)
                    .select_from(self.chunks.join(self.documents))
                    .where(self.documents.c.active.is_(True))
                    .limit(1)
                )
                is not None
            )

    def save_document(self, source, revision, chunks, vectors):
        with self.connection() as connection:
            self.check_profile(connection)

            # 同一文档串行更新。
            # 新版本写入失败时，事务回滚，旧版本仍然有效。
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:id))"),
                {"id": source.id},
            )

            if connection.scalar(
                select(self.documents.c.active).where(
                    self.documents.c.revision == revision
                )
            ):
                return False

            exists = connection.scalar(
                select(self.documents.c.revision).where(
                    self.documents.c.revision == revision
                )
            )

            connection.execute(
                update(self.documents)
                .where(
                    self.documents.c.source_id == source.id,
                    self.documents.c.active.is_(True),
                )
                .values(active=False)
            )

            if exists:
                connection.execute(
                    update(self.documents)
                    .where(self.documents.c.revision == revision)
                    .values(active=True)
                )
            else:
                connection.execute(
                    insert(self.documents).values(
                        revision=revision,
                        source_id=source.id,
                        active=True,
                        source=source.model_dump(exclude={"score"}),
                    )
                )

                connection.execute(
                    insert(self.chunks),
                    [
                        {
                            "id": digest(
                                [
                                    revision,
                                    chunk["start"],
                                    chunk["end"],
                                ]
                            ),
                            "revision": revision,
                            **chunk,
                            "embedding": vector,
                        }
                        for chunk, vector in zip(chunks, vectors)
                    ],
                )

            return True

    async def import_sources(self, sources):
        sources = list(sources)

        if len({source.id for source in sources}) != len(sources):
            raise ValueError("导入文件有重复文档ID。")

        if any(not source.text.strip() for source in sources):
            raise ValueError("导入文档正文不能为空。")

        await asyncio.to_thread(self.status)

        added, skipped = 0, 0

        for source in sources:
            revision = digest(
                {
                    "source": source.model_dump(exclude={"score"}),
                    "chunker": [
                        "chars-v1",
                        self.size,
                        self.overlap,
                    ],
                }
            )

            if await asyncio.to_thread(self.is_current, revision):
                skipped += 1
                continue

            chunks = split_text(
                source.text,
                self.size,
                self.overlap,
            )

            vectors = []

            for offset in range(0, len(chunks), 16):
                batch = chunks[offset : offset + 16]

                received = await self.client.embed(
                    [source.title + "\n" + chunk["text"] for chunk in batch]
                )

                check_vectors(
                    received,
                    len(batch),
                    self.settings.dimensions,
                )

                vectors.extend(received)

            changed = await asyncio.to_thread(
                self.save_document,
                source,
                revision,
                chunks,
                vectors,
            )

            added += int(changed)
            skipped += int(not changed)

        return {
            "updated": added,
            "skipped": skipped,
            **await asyncio.to_thread(self.status),
        }

    def find(self, vector, top_k):
        distance = self.chunks.c.embedding.cosine_distance(vector)

        query = (
            select(
                self.documents.c.source,
                self.chunks.c.id,
                self.chunks.c.start,
                self.chunks.c.end,
                self.chunks.c.text,
                distance.label("distance"),
            )
            .select_from(self.chunks.join(self.documents))
            .where(self.documents.c.active.is_(True))
            .order_by(distance)
            .limit(top_k)
        )

        with self.connection() as connection:
            self.check_profile(connection)
            rows = connection.execute(query).mappings().all()

        results = []

        for row in rows:
            score = 1 - float(row["distance"])

            if score < self.min_score:
                continue

            data = dict(row["source"])

            data.update(
                id="KB-" + row["id"][:24],
                text=row["text"],
                score=round(score, 4),
                title=(f'{data["title"]}' f'（字符{row["start"]}:{row["end"]}）'),
            )

            results.append(Source.model_validate(data))

        return results

    async def search(self, query, top_k=3):
        if (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 400
            or not 1 <= top_k <= 10
        ):
            raise ValueError("查询长度为1—400字符，top_k为1—10。")

        if not await asyncio.to_thread(self.ready):
            return []

        vectors = await self.client.embed([query.strip()])

        check_vectors(
            vectors,
            1,
            self.settings.dimensions,
        )

        return await asyncio.to_thread(
            self.find,
            vectors[0],
            top_k,
        )

    def close(self):
        self.engine.dispose()
