import asyncio
import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager

import jieba
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .providers import ProviderError
from .rag import PgKnowledgeSearch
from .schemas import Source

STOPWORDS = {
    "的",
    "了",
    "吗",
    "呢",
    "是",
    "什么",
    "怎么",
    "如何",
    "请",
    "帮",
    "我",
    "我们",
    "你",
    "一下",
    "相关",
    "资料",
    "本地",
    "知识库",
    "检索",
    "查询",
    "告诉",
    "介绍",
    "说明",
    "想",
    "了解",
}

TOKENIZER = jieba.Tokenizer()

PARSER_VERSION = hashlib.sha256(
    json.dumps(
        {
            "algorithm": 1,
            "jieba": jieba.__version__,
            "stopwords": sorted(STOPWORDS),
        },
        ensure_ascii=False,
    ).encode("utf-8")
).hexdigest()

logger = logging.getLogger(__name__)


def terms(value):
    return [
        word.lower()
        for word in TOKENIZER.lcut_for_search(value)
        if re.fullmatch(r"[A-Za-z0-9\u3400-\u9fff]+", word)
        and word.lower() not in STOPWORDS
    ]


def keyword_query(query):
    words = list(dict.fromkeys(terms(query)))[:16]

    return " OR ".join(f'"{word}"' for word in words)


def fuse_rrf(ranked_lists, top_k, rank_constant=60):
    if top_k < 1 or rank_constant < 1:
        raise ValueError("top_k 和 rank_constant 必须为正数")

    scores = {}
    sources = {}
    best = {}

    for items in ranked_lists:
        seen = set()
        rank = 0

        for source in items:
            if source.id in seen:
                continue

            seen.add(source.id)
            rank += 1

            sources.setdefault(source.id, source)

            scores[source.id] = scores.get(source.id, 0.0) + 1 / (rank_constant + rank)

            best[source.id] = min(
                best.get(source.id, rank),
                rank,
            )

    ordered = sorted(
        scores,
        key=lambda key: (
            -scores[key],
            best[key],
            key,
        ),
    )

    return [
        sources[key].model_copy(
            update={
                "score": round(scores[key], 6),
            }
        )
        for key in ordered[:top_k]
    ]


class KeywordIndex:
    def __init__(self, url=None):
        url = url or os.getenv("RAG_DATABASE_URL", "")

        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("RAG_DATABASE_URL 必须使用 postgresql+psycopg")

        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"connect_timeout": 5},
        )

    @contextmanager
    def connection(self, timeout_ms=30000):
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text("SELECT set_config(" "'statement_timeout', :value, true)"),
                    {"value": str(timeout_ms)},
                )

                yield connection

        except SQLAlchemyError:
            raise ProviderError(
                "keyword_database",
                "关键词索引访问失败，请检查数据库或重建索引。",
            ) from None

    def create_tables(self, connection):
        connection.execute(text("""
                CREATE TABLE IF NOT EXISTS rag_keyword_index (
                    chunk_id TEXT PRIMARY KEY
                        REFERENCES rag_chunks(id)
                        ON DELETE CASCADE,
                    parser_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    search_vector TSVECTOR NOT NULL
                )
                """))

        connection.execute(text("""
                CREATE INDEX IF NOT EXISTS rag_keyword_gin
                ON rag_keyword_index
                USING GIN(search_vector)
                """))

    def initialize(self):
        with self.connection() as connection:
            connection.execute(text("""
                    SELECT pg_advisory_xact_lock(
                        hashtext('carebrief_keyword_build')
                    )
                    """))

            self.create_tables(connection)

    def build(self):
        updated = 0
        skipped = 0

        with self.connection() as connection:
            acquired = connection.scalar(text("""
                    SELECT pg_try_advisory_xact_lock(
                        hashtext('carebrief_keyword_build')
                    )
                    """))

            if not acquired:
                raise ProviderError(
                    "keyword_busy",
                    "另一个关键词索引构建任务正在运行，请稍后重试。",
                )

            self.create_tables(connection)
            after = ""

            while True:
                rows = (
                    connection.execute(
                        text("""
                        SELECT
                            c.id,
                            c.text,
                            d.source->>'title' AS title,
                            d.source->>'institution' AS institution,
                            k.content_hash AS stored_hash
                        FROM rag_chunks c
                        JOIN rag_documents d
                            ON d.revision = c.revision
                        LEFT JOIN rag_keyword_index k
                            ON k.chunk_id = c.id
                        WHERE d.active = true
                          AND c.id > :after
                        ORDER BY c.id
                        LIMIT 200
                        """),
                        {"after": after},
                    )
                    .mappings()
                    .all()
                )

                if not rows:
                    break

                batch = []

                for row in rows:
                    title = row["title"] or ""
                    institution = row["institution"] or ""

                    fingerprint = hashlib.sha256(
                        (
                            f"{PARSER_VERSION}\0"
                            f"{title}\0"
                            f"{institution}\0"
                            f"{row['text']}"
                        ).encode("utf-8")
                    ).hexdigest()

                    if row["stored_hash"] == fingerprint:
                        skipped += 1
                        continue

                    batch.append(
                        {
                            "id": row["id"],
                            "version": PARSER_VERSION,
                            "hash": fingerprint,
                            "title": " ".join(terms(title + " " + institution)),
                            "body": " ".join(terms(row["text"])),
                        }
                    )

                if batch:
                    connection.execute(
                        text("""
                            INSERT INTO rag_keyword_index (
                                chunk_id,
                                parser_version,
                                content_hash,
                                search_vector
                            )
                            VALUES (
                                :id,
                                :version,
                                :hash,
                                setweight(
                                    to_tsvector('simple', :title),
                                    'A'
                                )
                                ||
                                setweight(
                                    to_tsvector('simple', :body),
                                    'B'
                                )
                            )
                            ON CONFLICT (chunk_id)
                            DO UPDATE SET
                                parser_version =
                                    EXCLUDED.parser_version,
                                content_hash =
                                    EXCLUDED.content_hash,
                                search_vector =
                                    EXCLUDED.search_vector
                            """),
                        batch,
                    )

                    updated += len(batch)

                after = rows[-1]["id"]

        return {
            "updated": updated,
            "skipped": skipped,
            **self.status(),
        }

    def status(self):
        with self.connection() as connection:
            exists = connection.scalar(text("SELECT to_regclass('rag_keyword_index')"))

            if not exists:
                return {
                    "keyword_ready": False,
                    "indexed_chunks": 0,
                }

            row = (
                connection.execute(
                    text("""
                    SELECT
                        count(*) AS total,
                        count(k.chunk_id) FILTER (
                            WHERE k.parser_version = :version
                        ) AS indexed
                    FROM rag_chunks c
                    JOIN rag_documents d
                        ON d.revision = c.revision
                    LEFT JOIN rag_keyword_index k
                        ON k.chunk_id = c.id
                    WHERE d.active = true
                    """),
                    {"version": PARSER_VERSION},
                )
                .mappings()
                .one()
            )

            return {
                "keyword_ready": (row["total"] > 0 and row["total"] == row["indexed"]),
                "active_chunks": row["total"],
                "indexed_chunks": row["indexed"],
                "pending_chunks": (row["total"] - row["indexed"]),
            }

    def search(self, query, top_k=10):
        expression = keyword_query(query)

        if not expression:
            return []

        with self.connection(timeout_ms=5000) as connection:
            exists = connection.scalar(text("SELECT to_regclass('rag_keyword_index')"))

            if not exists:
                raise ProviderError(
                    "keyword_missing",
                    "请先执行 python -m scripts.hybrid build。",
                )

            rows = (
                connection.execute(
                    text("""
                    WITH q AS (
                        SELECT websearch_to_tsquery(
                            'simple', :query
                        ) AS value
                    )
                    SELECT
                        c.id,
                        c.text,
                        c.start,
                        c."end",
                        d.source,
                        ts_rank_cd(
                            k.search_vector,
                            q.value,
                            32
                        ) AS rank
                    FROM rag_keyword_index k
                    JOIN rag_chunks c
                        ON c.id = k.chunk_id
                    JOIN rag_documents d
                        ON d.revision = c.revision
                    CROSS JOIN q
                    WHERE d.active = true
                      AND k.parser_version = :version
                      AND k.search_vector @@ q.value
                    ORDER BY rank DESC, c.id
                    LIMIT :limit
                    """),
                    {
                        "query": expression,
                        "version": PARSER_VERSION,
                        "limit": top_k,
                    },
                )
                .mappings()
                .all()
            )

        result = []

        for row in rows:
            source = Source.model_validate(row["source"])

            result.append(
                source.model_copy(
                    update={
                        # 与现有向量检索使用相同的分块引用 ID。
                        "id": "KB-" + row["id"][:24],
                        "text": row["text"],
                        "title": (
                            f"{source.title}" f"（字符{row['start']}:{row['end']}）"
                        ),
                        "score": round(float(row["rank"]), 6),
                    }
                )
            )

        return result

    def close(self):
        self.engine.dispose()


class HybridKnowledgeSearch(PgKnowledgeSearch):
    def __init__(self, settings, client, url=None, mode=None):
        self.mode = mode or os.getenv(
            "RAG_SEARCH_MODE",
            "hybrid",
        )

        if self.mode not in {"dense", "keyword", "hybrid"}:
            raise ValueError("RAG_SEARCH_MODE 只能是 dense/keyword/hybrid")

        self.candidates = int(os.getenv("RAG_HYBRID_CANDIDATES", "10"))

        if not 1 <= self.candidates <= 10:
            raise ValueError("RAG_HYBRID_CANDIDATES 必须在 1 到 10 之间")

        super().__init__(settings, client)
        self.keyword = KeywordIndex(url)

    def initialize(self):
        result = super().initialize()
        self.keyword.initialize()
        return result

    async def import_sources(self, sources):
        result = await super().import_sources(sources)

        result["keyword_index"] = await asyncio.to_thread(self.keyword.build)

        return result

    async def search_detailed(self, query, top_k=3):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 400:
            raise ValueError("query 必须为 1 到 400 字的非空字符串")

        if type(top_k) is not int or not 1 <= top_k <= 10:
            raise ValueError("top_k 必须在 1 到 10 之间")

        limit = max(top_k, self.candidates)

        if self.mode == "dense":
            dense = await super().search(query, limit)
            keyword = []
            warnings = []

        elif self.mode == "keyword":
            dense = []

            keyword = await asyncio.to_thread(
                self.keyword.search,
                query,
                limit,
            )

            warnings = []

        else:
            replies = await asyncio.gather(
                super().search(query, limit),
                asyncio.to_thread(
                    self.keyword.search,
                    query,
                    limit,
                ),
                return_exceptions=True,
            )

            failures = []
            warnings = []
            lists = []

            for name, reply in zip(
                ("dense", "keyword"),
                replies,
            ):
                if isinstance(reply, BaseException):
                    if not isinstance(reply, ProviderError):
                        raise reply

                    if reply.code == "rag_profile":
                        raise reply

                    failures.append(reply)
                    warnings.append(f"{name}: {reply.code}")
                    lists.append([])

                else:
                    lists.append(reply)

            dense, keyword = lists

            if failures and not dense and not keyword:
                raise failures[0]

        if self.mode == "hybrid":
            results = fuse_rrf(
                [dense, keyword],
                top_k,
            )
        else:
            results = (dense or keyword)[:top_k]

        logger.info(
            "retrieval mode=%s dense=%d keyword=%d warnings=%s",
            self.mode,
            len(dense),
            len(keyword),
            warnings,
        )

        if warnings:
            logger.warning(
                "混合检索已降级: %s",
                warnings,
            )

        return {
            "mode": self.mode,
            "dense": dense,
            "keyword": keyword,
            "warnings": warnings,
            "results": results,
        }

    async def search(self, query, top_k=3):
        detail = await self.search_detailed(
            query,
            top_k,
        )

        return detail["results"]

    def close(self):
        try:
            self.keyword.close()
        finally:
            super().close()
