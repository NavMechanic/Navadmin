"""N4 tables on the shared N0 ``Base`` (PostgreSQL in production, SQLite in dev/test).

Every N4 table is append-only, enforced by database triggers. Status changes (SOP approval,
calibration revocation, CAPA verification, invalidation) are new rows, never updates. Raw evidence
(SCPI / CSV / Parquet logs, waveforms, AOI images, lab PDFs, calibration certificates) lives in N0's
write-once object store, keyed by tenant + SHA-256; these tables hold the verified records.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from ..db import Base, JSONType, append_only_ddl
from ..models import utcnow


class _Rec:
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


def _id(n: int = 64) -> Mapped[str]:
    return mapped_column(String(n), primary_key=True)


def _col(n: int = 64, index: bool = False) -> Mapped[str]:
    return mapped_column(String(n), nullable=False, index=index)


class N4IntakeRow(_Rec, Base):
    __tablename__ = "n4_intakes"
    intake_id: Mapped[str] = _id()
    prd_id: Mapped[str] = _col(128)
    prd_revision: Mapped[str] = _col(16)
    design_revision: Mapped[str] = _col(16)
    __table_args__ = (Index("ix_n4_intake_prd", "tenant_id", "prd_id"),)


class N4PlanRow(_Rec, Base):
    __tablename__ = "n4_plans"
    plan_id: Mapped[str] = _id()
    intake_id: Mapped[str] = _col(index=True)
    plan_sha256: Mapped[str] = _col()


class N4MethodRow(_Rec, Base):
    __tablename__ = "n4_methods"
    method_id: Mapped[str] = _id(128)
    revision: Mapped[str] = _id(16)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = _id(128)
    status: Mapped[str] = _col(16)


class N4InstrumentRow(_Rec, Base):
    __tablename__ = "n4_instruments"
    instrument_id: Mapped[str] = _id(128)
    tenant_id: Mapped[str] = _id(128)
    serial: Mapped[str] = _col(128)


class N4CalibrationRow(_Rec, Base):
    __tablename__ = "n4_calibrations"
    calibration_id: Mapped[str] = _id(128)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = _id(128)
    instrument_id: Mapped[str] = _col(128, index=True)
    status: Mapped[str] = _col(16)


class N4LabRow(_Rec, Base):
    __tablename__ = "n4_labs"
    lab_id: Mapped[str] = _id(128)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = _id(128)


class N4QualificationRow(_Rec, Base):
    __tablename__ = "n4_qualifications"
    principal_id: Mapped[str] = _id(128)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = _id(128)


class N4PfmeaRow(_Rec, Base):
    __tablename__ = "n4_pfmea"
    pfmea_id: Mapped[str] = _id()
    plan_id: Mapped[str] = _col(index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class N4ControlPlanRow(_Rec, Base):
    __tablename__ = "n4_control_plans"
    control_plan_id: Mapped[str] = _id()
    plan_id: Mapped[str] = _col(index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class N4CapaRow(_Rec, Base):
    __tablename__ = "n4_capas"
    capa_id: Mapped[str] = _id(128)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = _id(128)
    plan_id: Mapped[str] = _col(index=True)
    status: Mapped[str] = _col(24)


class N4EvidenceRow(_Rec, Base):
    __tablename__ = "n4_evidence"
    evidence_id: Mapped[str] = _id()
    plan_id: Mapped[str] = _col(index=True)
    adapter: Mapped[str] = _col(16)
    status: Mapped[str] = _col(16)


class N4MeasurementRow(_Rec, Base):
    __tablename__ = "n4_measurements"
    measurement_id: Mapped[str] = _id(128)
    plan_id: Mapped[str] = _col(index=True)
    evidence_id: Mapped[str] = _col()
    test_id: Mapped[str] = _col(128)


class N4AdjudicationRow(_Rec, Base):
    __tablename__ = "n4_adjudications"
    adjudication_id: Mapped[str] = _id()
    evidence_id: Mapped[str] = _col(index=True)


class N4AssessmentRow(_Rec, Base):
    __tablename__ = "n4_assessments"
    assessment_id: Mapped[str] = _id()
    plan_id: Mapped[str] = _col(index=True)
    decision: Mapped[str] = _col(8)


class N4ReviewRow(_Rec, Base):
    __tablename__ = "n4_reviews"
    review_id: Mapped[str] = _id()
    assessment_id: Mapped[str] = _col(index=True)
    decision: Mapped[str] = _col(16)


class N4ReleaseRow(_Rec, Base):
    __tablename__ = "n4_releases"
    release_id: Mapped[str] = _id()
    plan_id: Mapped[str] = _col(index=True)


class N4CorpusRow(_Rec, Base):
    __tablename__ = "n4_corpus"
    doc_id: Mapped[str] = _id(128)
    tenant_id: Mapped[str] = _id(128)


class N4SuggestionRow(_Rec, Base):
    __tablename__ = "n4_suggestions"
    suggestion_id: Mapped[str] = _id()
    plan_id: Mapped[str] = _col(index=True)


class N4ReliabilityRow(_Rec, Base):
    __tablename__ = "n4_reliability"
    illustration_id: Mapped[str] = _id()
    plan_id: Mapped[str] = _col(index=True)


class N4EventRow(_Rec, Base):
    __tablename__ = "n4_events"
    event_id: Mapped[str] = _id()
    subject_kind: Mapped[str] = _col(32)
    subject_id: Mapped[str] = _col(128)
    status: Mapped[str] = _col(32)
    __table_args__ = (Index("ix_n4_events_subject", "tenant_id", "subject_kind", "subject_id"),)


N4_APPEND_ONLY_TABLES = tuple(sorted(t for t in Base.metadata.tables if t.startswith("n4_")))

for _name in N4_APPEND_ONLY_TABLES:
    _table = Base.metadata.tables[_name]
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(_table, "after_create", DDL(_stmt).execute_if(dialect=_dialect))
