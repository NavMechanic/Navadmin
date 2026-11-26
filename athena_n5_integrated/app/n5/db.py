"""N5 tables on the shared N0 ``Base`` (PostgreSQL in production, SQLite in dev / test).

Every N5 table is append-only, enforced by database triggers: qualification decisions, staleness,
revocation blocks and supersession are new rows (``n5_events`` / new ``seq``), never updates. Signed
quotation files live in N0's write-once object store keyed by tenant + SHA-256 (the quote vault).
Distributor API payloads are never stored: ``n5_connector_calls`` keeps call metadata and the
response hash only.
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


class N5IntakeRow(_Rec, Base):
    __tablename__ = "n5_intakes"
    intake_id: Mapped[str] = _id()
    prd_id: Mapped[str] = _col(128)
    bom_artifact: Mapped[str] = _col(160)
    design_revision: Mapped[str] = _col(16)
    __table_args__ = (Index("ix_n5_intake_prd", "tenant_id", "prd_id"),)


class N5SupplierRow(_Rec, Base):
    __tablename__ = "n5_suppliers"
    supplier_id: Mapped[str] = _id(128)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = _id(128)
    status: Mapped[str] = _col(16)


class N5QuoteRow(_Rec, Base):
    __tablename__ = "n5_quotes"
    quote_id: Mapped[str] = _id()
    bom_intake_id: Mapped[str] = _col(index=True)
    supplier_id: Mapped[str] = _col(128)
    source_id: Mapped[str] = _col(128, index=True)
    status: Mapped[str] = _col(16)


class N5FxRow(_Rec, Base):
    __tablename__ = "n5_fx"
    fx_id: Mapped[str] = _id(128)
    tenant_id: Mapped[str] = _id(128)
    pair: Mapped[str] = _col(8)


class N5ReportRow(_Rec, Base):
    __tablename__ = "n5_reports"
    report_id: Mapped[str] = _id()
    bom_intake_id: Mapped[str] = _col(index=True)
    decision: Mapped[str] = _col(8)


class N5ReviewRow(_Rec, Base):
    __tablename__ = "n5_reviews"
    review_id: Mapped[str] = _id()
    report_id: Mapped[str] = _col(index=True)


class N5HandoffRow(_Rec, Base):
    __tablename__ = "n5_handoffs"
    handoff_id: Mapped[str] = _id()
    report_id: Mapped[str] = _col(index=True)


class N5EcoRow(_Rec, Base):
    __tablename__ = "n5_eco_proposals"
    eco_id: Mapped[str] = _id()
    bom_intake_id: Mapped[str] = _col(index=True)


class N5ConnectorCallRow(_Rec, Base):
    __tablename__ = "n5_connector_calls"
    call_id: Mapped[str] = _id()
    connector: Mapped[str] = _col(24)


class N5EventRow(_Rec, Base):
    __tablename__ = "n5_events"
    event_id: Mapped[str] = _id()
    subject_kind: Mapped[str] = _col(16)
    subject_id: Mapped[str] = _col(index=True)
    status: Mapped[str] = _col(24)


N5_APPEND_ONLY_TABLES = tuple(sorted(t for t in Base.metadata.tables if t.startswith("n5_")))

for _name in N5_APPEND_ONLY_TABLES:
    _table = Base.metadata.tables[_name]
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(_table, "after_create", DDL(_stmt).execute_if(dialect=_dialect))
