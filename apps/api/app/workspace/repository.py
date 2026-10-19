# CAREBRIEF_CONTEXT_METRICS_V1
from app.context_compaction_metrics import context_compaction_metrics
# CAREBRIEF_RELIABILITY_V3
from ..agent_reliability_v3 import EVENT_FIELDS, preserve_event_details
# CAREBRIEF_RELIABILITY_V2
import math
import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (create_engine, select, update, delete, text, Table, Column,
                        MetaData, String, Text, Integer, JSON, ForeignKey, func)
from sqlalchemy.orm import Session
from pgvector.sqlalchemy import Vector

from .models import Base, Document, Selection, Run, RunEvent, Feedback
from .failures import analyze


def now():
    return datetime.now(timezone.utc).isoformat()


class WorkspaceError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class Repository:
    def __init__(self, url, dimensions, model, *, allow_sqlite=False):
        if not url:
            raise WorkspaceError("database_unavailable", 503)
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        elif url.startswith("postgresql+psycopg2://"):
            url = "postgresql+psycopg://" + url[len("postgresql+psycopg2://"):]
        if not url.startswith("postgresql+psycopg://") and not (allow_sqlite and url.startswith("sqlite")):
            raise WorkspaceError("database_unavailable", 503)
        self.dimensions, self.model = dimensions, model
        self.engine = create_engine(url, pool_pre_ping=True,
                                   connect_args={"check_same_thread": False} if allow_sqlite else {})
        meta = MetaData()
        Document.__table__.to_metadata(meta)
        self.chunks = Table(
            "cb_private_chunks", meta,
            Column("id", String(80), primary_key=True),
            Column("document_id", String(64), ForeignKey("cb_private_documents.id", ondelete="CASCADE"), nullable=False, index=True),
            Column("owner", String(64), nullable=False, index=True),
            Column("revision", Integer, nullable=False),
            Column("position", Integer, nullable=False),
            Column("location", String(80), nullable=False),
            Column("content", Text, nullable=False),
            Column("embedding_model", String(160), nullable=False),
            Column("embedding", Vector(dimensions).with_variant(JSON, "sqlite"), nullable=False),
        )

    def initialize(self):
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as db:
                db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                existing = db.execute(text("""
                    SELECT format_type(a.atttypid, a.atttypmod)
                    FROM pg_attribute a
                    WHERE a.attrelid = to_regclass('cb_private_chunks')
                      AND a.attname = 'embedding' AND NOT a.attisdropped
                """)).scalar()
                if existing and existing != f"vector({self.dimensions})":
                    raise WorkspaceError("index_model_changed", 503)
        Base.metadata.create_all(self.engine)
        self.chunks.metadata.create_all(self.engine, tables=[self.chunks])

    def recover(self):
        with Session(self.engine) as db:
            db.execute(update(Document).where(Document.status.in_(["queued", "parsing", "indexing"])).values(
                status="failed", error_code="index_interrupted", updated_at=now()))
            db.execute(update(Run).where(Run.status == "running").values(
                status="failed", error_code="process_interrupted", updated_at=now()))
            db.commit()

    @staticmethod
    def document_view(doc, include_pieces=False):
        value = {k: getattr(doc, k) for k in ("id", "name", "suffix", "size", "status", "revision",
                                            "error_code", "chunk_count", "created_at", "updated_at")}
        if include_pieces:
            value["pieces"] = doc.pieces
        return value

    @staticmethod
    def owned(db, owner, doc_id):
        doc = db.scalar(select(Document).where(Document.owner == owner, Document.id == doc_id,
                                              Document.status != "deleted"))
        if not doc:
            raise WorkspaceError("document_not_found", 404)
        return doc

    def duplicate(self, owner, digest):
        with Session(self.engine) as db:
            doc = db.scalar(select(Document).where(Document.owner == owner, Document.digest == digest,
                                                   Document.status != "deleted").limit(1))
            return self.document_view(doc) if doc else None

    def create_document(self, owner, name, suffix, key, digest, size):
        with Session(self.engine) as db:
            # A transaction-scoped per-owner lock serializes quota/dedup checks.
            if self.engine.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"), {"owner": owner})
            existing = db.scalar(select(Document).where(Document.owner == owner, Document.digest == digest,
                                                       Document.status != "deleted").limit(1))
            if existing:
                return self.document_view(existing), False
            count = db.scalar(select(func.count()).select_from(Document).where(
                Document.owner == owner, Document.status != "deleted"))
            if count >= 50:
                raise WorkspaceError("document_quota", 429)
            doc = Document(id=uuid4().hex, owner=owner, name=name, suffix=suffix, storage_key=key,
                           digest=digest, size=size, status="queued", revision=1, pieces=[],
                           created_at=now(), updated_at=now(), error_code="", chunk_count=0)
            db.add(doc)
            db.commit()
            return self.document_view(doc), True

    def documents(self, owner):
        with Session(self.engine) as db:
            rows = db.scalars(select(Document).where(Document.owner == owner, Document.status != "deleted")
                              .order_by(Document.created_at.desc()).limit(50))
            return [self.document_view(d) for d in rows]

    def document(self, owner, doc_id):
        with Session(self.engine) as db:
            doc = self.owned(db, owner, doc_id)
            value = self.document_view(doc, True)
            value["storage_key"] = doc.storage_key
            return value

    def transition(self, owner, doc_id, expected_revision, expected, status, **values):
        with Session(self.engine) as db:
            changed = db.execute(update(Document).where(Document.owner == owner, Document.id == doc_id,
                                 Document.revision == expected_revision, Document.status.in_(expected))
                                 .values(status=status, updated_at=now(), **values)).rowcount
            db.commit()
            return bool(changed)

    def confirm(self, owner, doc_id, revision, pieces):
        if not self.transition(owner, doc_id, revision, ["awaiting_confirmation", "ready", "failed"],
                               "indexing", revision=revision+1, pieces=pieces, error_code="", chunk_count=0):
            raise WorkspaceError("revision_conflict")
        return revision + 1

    def begin_retry(self, owner, doc_id, revision):
        doc = self.document(owner, doc_id)
        target = "indexing" if doc["pieces"] else "queued"
        if not self.transition(owner, doc_id, revision, ["failed"], target, revision=revision+1, error_code=""):
            raise WorkspaceError("revision_conflict")
        return target, revision + 1

    def save_chunks(self, owner, doc_id, revision, chunks, vectors):
        if len(chunks) != len(vectors) or not chunks:
            raise WorkspaceError("embedding_format", 503)
        if any(len(v) != self.dimensions or not all(math.isfinite(x) for x in v)
               or sum(x*x for x in v) == 0 for v in vectors):
            raise WorkspaceError("embedding_format", 503)
        with Session(self.engine) as db:
            # Acquire the row lock before publishing chunks and ready state atomically.
            changed = db.execute(update(Document).where(Document.owner == owner, Document.id == doc_id,
                                 Document.revision == revision, Document.status == "indexing")
                                 .values(status="ready", updated_at=now(), chunk_count=len(chunks), error_code="")).rowcount
            if not changed:
                db.rollback()
                return False
            db.execute(delete(self.chunks).where(self.chunks.c.document_id == doc_id, self.chunks.c.owner == owner))
            db.execute(self.chunks.insert(), [dict(
                id=f"PRIV-{doc_id}-{revision}-{i}", document_id=doc_id, owner=owner, revision=revision,
                position=i, location=c["location"], content=c["text"], embedding_model=self.model, embedding=v,
            ) for i, (c, v) in enumerate(zip(chunks, vectors))])
            db.commit()
            return True

    def delete_document(self, owner, doc_id):
        with Session(self.engine) as db:
            doc = self.owned(db, owner, doc_id)
            key = doc.storage_key
            # Revision change invalidates an in-flight parser or indexer.
            doc.status, doc.revision, doc.pieces, doc.chunk_count = "deleted", doc.revision + 1, [], 0
            doc.updated_at = now()
            db.execute(delete(self.chunks).where(self.chunks.c.document_id == doc_id, self.chunks.c.owner == owner))
            for selection in db.scalars(select(Selection).where(Selection.owner == owner)):
                if doc_id in selection.document_ids:
                    selection.document_ids = [key for key in selection.document_ids if key != doc_id]
            db.commit()
            return key

    def select_documents(self, owner, session_id, ids):
        with Session(self.engine) as db:
            for doc_id in ids:
                if self.owned(db, owner, doc_id).status != "ready":
                    raise WorkspaceError("document_not_ready")
            row = db.get(Selection, (owner, session_id))
            if row:
                row.document_ids = list(dict.fromkeys(ids))
            else:
                db.add(Selection(owner=owner, session_id=session_id, document_ids=list(dict.fromkeys(ids))))
            db.commit()

    def selected(self, owner, session_id):
        with Session(self.engine) as db:
            row = db.get(Selection, (owner, session_id))
            return list(row.document_ids) if row else []

    def search(self, owner, query, vector, top_k, document_ids=None):
        c = self.chunks.c
        filters = [c.owner == owner, Document.owner == owner, Document.status == "ready",
                   c.embedding_model == self.model, c.revision == Document.revision]
        if document_ids:
            filters.append(c.document_id.in_(document_ids))
        base = select(c.id, c.document_id, c.content, c.location, Document.name, c.embedding).join(
            Document, Document.id == c.document_id).where(*filters)
        with Session(self.engine) as db:
            stale = db.scalar(select(func.count()).select_from(self.chunks).join(Document,
                Document.id == c.document_id).where(c.owner == owner, Document.owner == owner,
                Document.status == "ready", c.embedding_model != self.model,
                *([c.document_id.in_(document_ids)] if document_ids else [])))
            if stale:
                raise WorkspaceError("index_model_changed", 503)
            if self.engine.dialect.name == "sqlite":
                # Explicit test-only exhaustive search; production uses pgvector.
                rows = list(db.execute(base).mappings())
                def distance(row):
                    v = row["embedding"]
                    return 1 - sum(a*b for a, b in zip(v, vector)) / math.sqrt(sum(a*a for a in v)*sum(b*b for b in vector))
                dense = sorted(rows, key=distance)[:20]
            else:
                dense = list(db.execute(base.order_by(c.embedding.cosine_distance(vector)).limit(20)).mappings())
            words = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{2,}", query.lower())
            tokens = list(dict.fromkeys(t for w in words for t in ([w] if not re.match(r"[\u4e00-\u9fff]", w)
                          else [w[i:i+2] for i in range(len(w)-1)])))[:24]
            if tokens:
                score = sum(c.content.contains(t, autoescape=True).cast(Integer) for t in tokens)
                keyword = list(db.execute(base.where(score > 0).order_by(score.desc(), c.id).limit(20)).mappings())
            else:
                keyword = []
        scores, found = {}, {}
        for ranking in (dense, keyword):
            for rank, row in enumerate(ranking, 1):
                found[row["id"]] = row
                scores[row["id"]] = scores.get(row["id"], 0) + 1 / (60 + rank)
        return [{k: v for k, v in found[key].items() if k != "embedding"} | {"score": scores[key]}
                for key in sorted(scores, key=lambda k: (-scores[k], k))[:top_k]]

    def begin_run(self, owner, session_id, run_id):
        with Session(self.engine) as db:
            db.add(Run(id=run_id, owner=owner, session_id=session_id, status="running", created_at=now(), updated_at=now()))
            db.commit()

    def record_event(self, run_id, event):
        allowed = EVENT_FIELDS
        # CAREBRIEF_CONTEXT_METRICS_ORDER_V2
        context_metrics = context_compaction_metrics(event)
        event = preserve_event_details(event)
        with Session(self.engine) as db:
            if not db.get(Run, run_id):
                return
            db.add(RunEvent(run_id=run_id, payload={"time": now(), **{k:v for k,v in event.items() if k in allowed}, **context_metrics}))
            db.commit()

    def finish_run(self, run_id, status, code, duration):
        with Session(self.engine) as db:
            db.execute(update(Run).where(Run.id == run_id).values(status=status, error_code=code,
                       duration_ms=duration, updated_at=now()))
            db.commit()

    def runs(self, owner, session_id=None, admin=False):
        with Session(self.engine) as db:
            query = select(Run).order_by(Run.created_at.desc()).limit(100)
            if not admin:
                query = query.where(Run.owner == owner)
            if session_id:
                query = query.where(Run.session_id == session_id)
            return [{k:getattr(r,k) for k in ("id", "session_id", "status", "error_code", "duration_ms", "created_at")}
                    for r in db.scalars(query)]

    def run_detail(self, owner, run_id, admin=False):
        with Session(self.engine) as db:
            r = db.scalar(select(Run).where(Run.id == run_id, *([] if admin else [Run.owner == owner])))
            if not r:
                raise WorkspaceError("run_not_found", 404)
            events = list(db.scalars(select(RunEvent.payload).where(RunEvent.run_id == run_id).order_by(RunEvent.id)))
            return {"id": r.id, "status": r.status, "error_code": r.error_code, "events": events,
                    "analysis": analyze(r.error_code, events)}

    def feedback(self, owner, run_id, category, comment):
        self.run_detail(owner, run_id)
        with Session(self.engine) as db:
            db.add(Feedback(id=uuid4().hex, owner=owner, run_id=run_id, category=category,
                            comment=comment, created_at=now()))
            db.commit()
