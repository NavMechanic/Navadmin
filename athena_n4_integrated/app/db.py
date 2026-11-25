"""Durable storage: SQLAlchemy 2.0 models for PostgreSQL (production) and SQLite (dev/test).

Audit-bearing tables are append-only *in the database*, not just in code: a trigger rejects
UPDATE and DELETE on ``audit_events``, ``approval_events``, ``decisions``, ``release_manifests``,
``model_runs``, ``feedback`` and ``lineage_edges`` (PL/pgSQL on PostgreSQL, RAISE(ABORT) on
SQLite). Audit and approval events are additionally hash-chained per tenant.

Schema changes go through Alembic (``alembic upgrade head``); ``Database.create_all`` exists for
tests and local development (``N0_AUTO_CREATE_SCHEMA=true``).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from sqlalchemy import (JSON, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, create_engine,
                        event, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.schema import DDL

from .models import utcnow

JSONType = JSON().with_variant(JSONB(), "postgresql")
APPEND_ONLY_TABLES = ("audit_events", "approval_events", "decisions", "release_manifests", "model_runs", "feedback",
                      "lineage_edges")


class Base(DeclarativeBase):
    pass


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SourceRow(Base):
    __tablename__ = "sources"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    registered_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class BlobRow(Base):
    __tablename__ = "blobs"
    blob_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)          # UPLOAD | PAYLOAD
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    declared_mime: Mapped[str | None] = mapped_column(String(128))
    sniffed_mime: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    scan: Mapped[dict] = mapped_column(JSONType, nullable=False)
    bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("tenant_id", "sha256", "kind", name="uq_blob_tenant_sha_kind"),)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[str] = mapped_column(String(16), nullable=False)
    revision_order: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    release_stage: Mapped[str] = mapped_column(String(16), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted: Mapped[dict] = mapped_column(JSONType, nullable=False)
    canonical: Mapped[dict] = mapped_column(JSONType, nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    origin_node: Mapped[str] = mapped_column(String(4), nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("tenant_id", "artifact_id", "revision", name="uq_artifact_revision"),
                      Index("ix_artifact_sha", "tenant_id", "source_sha256"))


class DecisionRow(Base):
    __tablename__ = "decisions"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    validator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_decision_artifact", "tenant_id", "artifact_id", "revision", "seq"),)


class ReleaseRow(Base):
    __tablename__ = "release_manifests"
    manifest_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_release_artifact", "tenant_id", "artifact_id", "revision"),)


class ModelRunRow(Base):
    __tablename__ = "model_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class ApprovalRow(Base):
    __tablename__ = "approval_events"
    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(160), nullable=False)
    exact_revision: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    approver_id: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_approval_artifact", "tenant_id", "artifact_id", "exact_revision"),)


class AuditRow(Base):
    __tablename__ = "audit_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subject: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _ts()


class ChainHeadRow(Base):
    __tablename__ = "chain_heads"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), primary_key=True)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class FeedbackRow(Base):
    __tablename__ = "feedback"
    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[str] = mapped_column(String(16), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    training_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = _ts()


class LineageRow(Base):
    __tablename__ = "lineage_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(256), nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    relation: Mapped[str] = mapped_column(String(48), nullable=False)
    attrs: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_lineage_source", "tenant_id", "source"), Index("ix_lineage_target", "tenant_id", "target"))


class WorkPackageRow(Base):
    __tablename__ = "vendor_work_packages"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    work_package_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    vendor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)       # AUTHORIZED | BLOCKED
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    decision: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _ts()


class VendorResultRow(Base):
    __tablename__ = "vendor_results"
    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    work_package_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


# ----------------------------------------------------------------------------------------------
# append-only triggers
# ----------------------------------------------------------------------------------------------
PG_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION n0_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (ATHENA N0 audit invariant)', TG_TABLE_NAME;
END; $$ LANGUAGE plpgsql;
"""


def append_only_ddl(table: str, dialect: str) -> list[str]:
    if dialect == "postgresql":
        return [PG_APPEND_ONLY_FUNCTION,
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION n0_append_only();"]
    if dialect == "sqlite":
        return [f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;",
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"]
    return []


for _name in APPEND_ONLY_TABLES:
    _table = Base.metadata.tables[_name]
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(_table, "after_create", DDL(_stmt).execute_if(dialect=_dialect))


# ----------------------------------------------------------------------------------------------
class Database:
    def __init__(self, url: str) -> None:
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                from pathlib import Path
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, future=True, pool_pre_ping=True, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _):          # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
        self.SessionLocal = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def ping(self) -> dict:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                tables = set(self.engine.dialect.get_table_names(conn))
            missing = sorted(set(Base.metadata.tables) - tables)
            return {"ok": not missing, "dialect": self.engine.dialect.name, "missing_tables": missing}
        except Exception as exc:  # pragma: no cover - depends on the database
            return {"ok": False, "dialect": self.engine.dialect.name, "error": f"{type(exc).__name__}: {exc}"}
