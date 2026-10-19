from sqlalchemy import Column, String, Text, Integer, JSON, ForeignKey, Index
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Document(Base):
    __tablename__ = "cb_private_documents"
    id = Column(String(64), primary_key=True)
    owner = Column(String(64), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    suffix = Column(String(12), nullable=False)
    storage_key = Column(String(80), nullable=False)
    digest = Column(String(64), nullable=False)
    size = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    pieces = Column(JSON, nullable=False, default=list)
    error_code = Column(String(80), nullable=False, default="")
    chunk_count = Column(Integer, nullable=False, default=0)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    __table_args__ = (Index("cb_private_owner_digest", "owner", "digest"),)


class Selection(Base):
    __tablename__ = "cb_private_selections"
    owner = Column(String(64), primary_key=True)
    session_id = Column(String(80), primary_key=True)
    document_ids = Column(JSON, nullable=False, default=list)


class Run(Base):
    __tablename__ = "cb_workspace_runs"
    id = Column(String(80), primary_key=True)
    owner = Column(String(64), nullable=False, index=True)
    session_id = Column(String(80), nullable=False, index=True)
    status = Column(String(24), nullable=False)
    error_code = Column(String(80), nullable=False, default="")
    duration_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)


class RunEvent(Base):
    __tablename__ = "cb_workspace_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(80), ForeignKey("cb_workspace_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    payload = Column(JSON, nullable=False)


class Feedback(Base):
    __tablename__ = "cb_workspace_feedback"
    id = Column(String(64), primary_key=True)
    owner = Column(String(64), nullable=False, index=True)
    run_id = Column(String(80), ForeignKey("cb_workspace_runs.id", ondelete="CASCADE"), nullable=False)
    category = Column(String(40), nullable=False)
    comment = Column(Text, nullable=False, default="")
    created_at = Column(String(40), nullable=False)
