"""N1 tables on the shared N0 ``Base`` (PostgreSQL in production, SQLite in dev/test).

Every N1 table is append-only, enforced by the same database triggers N0 uses. Changes are new
revisions, never updates:

  n1_batches             one row per N0 artifact revision N1 ingested (pinned to the N0 decision)
  n1_observations        MarketObservation revisions (evidence_id, revision) + content hash
  n1_buyer_accounts      BuyerAccount revisions (account_id, revision)
  n1_attestations        EvidenceAttestation, bound to the evidence content hash
  n1_reports             MarketReport revisions per scope (scope_key, revision) + report hash
  n1_feedback_cases      reviewer labels and corrections
  n1_connector_previews  staged connector output with the raw-payload hash
  n1_optimizations       N1.5 results (never executed)
  n1_slm_evaluations     gold-set qualification runs
  n1_handoffs            N1 → N2/N6/N7 handoff results with their N0 artifact / manifest ids
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from ..db import Base, JSONType, append_only_ddl
from ..models import utcnow


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class N1BatchRow(Base):
    __tablename__ = "n1_batches"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    n0_artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    n0_revision: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    n0_decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N1ObservationRow(Base):
    __tablename__ = "n1_observations"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    n0_artifact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    n0_revision: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(64))
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_n1_obs_account", "tenant_id", "account_id"),)


class N1AccountRow(Base):
    __tablename__ = "n1_buyer_accounts"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N1AttestationRow(Base):
    __tablename__ = "n1_attestations"
    attestation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_n1_att_evidence", "tenant_id", "evidence_id"),)


class N1ReportRow(Base):
    __tablename__ = "n1_reports"
    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_report_id: Mapped[str | None] = mapped_column(String(64))
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("tenant_id", "scope_key", "revision", name="uq_n1_report_scope_rev"),)


class N1FeedbackRow(Base):
    __tablename__ = "n1_feedback_cases"
    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    training_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N1PreviewRow(Base):
    __tablename__ = "n1_connector_previews"
    preview_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    connector: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N1OptimizationRow(Base):
    __tablename__ = "n1_optimizations"
    optimization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    report_id: Mapped[str | None] = mapped_column(String(64))
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N1SlmEvaluationRow(Base):
    __tablename__ = "n1_slm_evaluations"
    evaluation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    gold_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N1HandoffRow(Base):
    __tablename__ = "n1_handoffs"
    handoff_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    report_id: Mapped[str] = mapped_column(String(64), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


N1_APPEND_ONLY_TABLES = ("n1_batches", "n1_observations", "n1_buyer_accounts", "n1_attestations", "n1_reports",
                         "n1_feedback_cases", "n1_connector_previews", "n1_optimizations", "n1_slm_evaluations",
                         "n1_handoffs")

for _name in N1_APPEND_ONLY_TABLES:
    _table = Base.metadata.tables[_name]
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(_table, "after_create", DDL(_stmt).execute_if(dialect=_dialect))
