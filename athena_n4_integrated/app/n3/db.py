"""N3 tables on the shared N0 ``Base`` (PostgreSQL in production, SQLite in dev/test).

Every N3 table is append-only, enforced by the same database triggers N0 uses. State changes
(job progress, invalidation after an N2 PRD revision) are new rows, never updates:

  n3_baselines        N3DesignBaseline locked to one N2 PRDRelease (PRD id + revision + SHA-256)
  n3_work_packages    immutable N3WorkPackage (+ package_sha256)
  n3_vendors          N3VendorProfile versions (vendor_id, version)
  n3_routes           N3Routing decisions (tool / vendor, N0 VendorWorkPackage.v1 id)
  n3_deliveries       N3VendorDelivery (accepted or rejected) with the manifest
  n3_files            N3FileEvidence per work package (vendor / engineer / tool-job files)
  n3_jobs             N3ToolJob status rows (job_id, seq): QUEUED → RUNNING → SUCCEEDED/FAILED/TIMEOUT
  n3_assessments      N3Assessment with findings and decision (+ assessment_sha256)
  n3_actions          N3Action rework proposals (never executed)
  n3_reviews          N3Review design-authority sign-off, bound to an approval-chain event
  n3_releases         N3Release with per-target N0 results
  n3_feedback         N3Feedback labels
  n3_events           status events (INVALIDATED …) for any N3 subject
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from ..db import Base, JSONType, append_only_ddl
from ..models import utcnow


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


def _tenant() -> Mapped[str]:
    return mapped_column(String(128), nullable=False)


class N3BaselineRow(Base):
    __tablename__ = "n3_baselines"
    baseline_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    prd_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prd_revision: Mapped[str] = mapped_column(String(16), nullable=False)
    prd_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_n3_baseline_prd", "tenant_id", "prd_id"),)


class N3WorkPackageRow(Base):
    __tablename__ = "n3_work_packages"
    work_package_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    baseline_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_n3_wp_baseline", "tenant_id", "baseline_id"),)


class N3VendorRow(Base):
    __tablename__ = "n3_vendors"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    vendor_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3RouteRow(Base):
    __tablename__ = "n3_routes"
    route_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    work_package_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    authorized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3DeliveryRow(Base):
    __tablename__ = "n3_deliveries"
    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    work_package_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3FileRow(Base):
    __tablename__ = "n3_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = _tenant()
    work_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_n3_files_wp", "tenant_id", "work_package_id"),)


class N3JobRow(Base):
    __tablename__ = "n3_jobs"
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    work_package_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3AssessmentRow(Base):
    __tablename__ = "n3_assessments"
    assessment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    work_package_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    assessment_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3ActionRow(Base):
    __tablename__ = "n3_actions"
    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    assessment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3ReviewRow(Base):
    __tablename__ = "n3_reviews"
    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    assessment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3ReleaseRow(Base):
    __tablename__ = "n3_releases"
    release_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    baseline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3FeedbackRow(Base):
    __tablename__ = "n3_feedback"
    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()


class N3EventRow(Base):
    __tablename__ = "n3_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = _tenant()
    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_n3_events_subject", "tenant_id", "subject_kind", "subject_id"),)


N3_APPEND_ONLY_TABLES = ("n3_baselines", "n3_work_packages", "n3_vendors", "n3_routes", "n3_deliveries", "n3_files",
                         "n3_jobs", "n3_assessments", "n3_actions", "n3_reviews", "n3_releases", "n3_feedback",
                         "n3_events")

for _name in N3_APPEND_ONLY_TABLES:
    _table = Base.metadata.tables[_name]
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(_table, "after_create", DDL(_stmt).execute_if(dialect=_dialect))
