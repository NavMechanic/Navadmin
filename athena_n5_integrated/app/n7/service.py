"""N7 intake of N5 sourcing handoffs as draft material cost records (append-only tables).

Only the slice N5 hands off to is implemented: quoted material cost lines, FX assumptions and quote
validity dates from a released, hash-verified, live SOURCING_HANDOFF. The record keeps the
indicative-material label: N7 still has to add GST, tooling / NRE, financing, freight and duties,
labour and warranty. Blocked handoffs mark the record BLOCKED (listener + live check).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, String, event, select
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from ..db import Base, JSONType, append_only_ddl
from ..errors import N0Error
from ..models import utcnow
from ..n5.downstream import verify_released_handoff
from ..n6.service import HandoffIntakeRequest
from ..repository import Repository, dump, new_id
from ..security import Principal


class N7IntakeRow(Base):
    __tablename__ = "n7_intakes"
    intake_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    handoff_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class N7EventRow(Base):
    __tablename__ = "n7_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    intake_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


N7_APPEND_ONLY_TABLES = ("n7_intakes", "n7_events")
for _name in N7_APPEND_ONLY_TABLES:
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(Base.metadata.tables[_name], "after_create", DDL(_stmt).execute_if(dialect=_dialect))


class N7CostRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", title="N7CostRecord")
    intake_id: str
    tenant_id: str
    artifact: str
    handoff_id: str
    bom_ref: str
    design_revision: str
    build_quantity: int
    base_currency: str
    cost_lines: list[dict[str, Any]]
    fx_assumptions: list[dict[str, Any]]
    indicative_total: str
    indicative_unit_cost: str
    earliest_quote_expiry: str
    label: str
    excluded_cost_categories: list[str]
    excluded_items_reported: list[dict[str, Any]]
    synthetic: bool
    verification_ref: Optional[str] = None
    status: Literal["DRAFT"] = "DRAFT"
    ingested_by: str
    ingested_at: datetime


N7_SCHEMAS = {"n7_cost_record": N7CostRecord}


class N7Service:
    def __init__(self, state) -> None:
        self.state, self.db, self.clock = state, state.db, state.clock
        state.n5.handoff_listeners.append(self.on_handoff_status)

    def ingest(self, p: Principal, req: HandoffIntakeRequest) -> N7CostRecord:
        t = p.tenant_id
        payload, h = verify_released_handoff(self.state, p, req.artifact_id, req.revision, "N7")
        ref = f"{req.artifact_id}@{req.revision}"
        with self.db.session() as s:
            if s.scalars(select(N7IntakeRow).where(N7IntakeRow.tenant_id == t, N7IntakeRow.artifact_ref == ref)).first():
                raise N0Error(409, "ALREADY_INGESTED", f"{ref} is already an N7 cost record")
            fin = payload.get("finance") or {}
            lines = [{k: ln.get(k) for k in ("line", "mpn", "supplier_id", "quote_id", "q_order", "unit_price",
                                             "currency", "line_charges", "extended_cost", "fx_id", "fx_rate",
                                             "extended_cost_base", "valid_until")} for ln in payload["lines"]]
            rec = N7CostRecord(intake_id=new_id("N7I"), tenant_id=t, artifact=ref, handoff_id=h.handoff_id,
                               bom_ref=payload["bom_ref"], design_revision=payload["design_revision"],
                               build_quantity=payload["build_quantity"], base_currency=payload["base_currency"],
                               cost_lines=lines, fx_assumptions=payload.get("fx_assumptions") or [],
                               indicative_total=str(payload["indicative_total"]),
                               indicative_unit_cost=str(payload["indicative_unit_cost"]),
                               earliest_quote_expiry=(fin.get("quote_validity") or {}).get("earliest_expiry")
                               or min(ln["valid_until"] for ln in lines),
                               label=payload["indicative_scope"],
                               excluded_cost_categories=payload["excluded_cost_categories"],
                               excluded_items_reported=payload.get("excluded_items_reported") or [],
                               synthetic=payload["_synthetic"], verification_ref=payload.get("verification_ref"),
                               ingested_by=p.principal_id, ingested_at=self.clock())
            s.add(N7IntakeRow(intake_id=rec.intake_id, tenant_id=t, artifact_ref=ref, handoff_id=h.handoff_id,
                              record=dump(rec)))
            repo = Repository(s)
            repo.add_edge(t, f"artifact:{ref}", f"n7_cost_record:{rec.intake_id}", "INGESTED_BY", node="N7")
            repo.append_audit(tenant_id=t, event_type="N7_COST_RECORD_INGESTED", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"n7_cost_record:{rec.intake_id}",
                              details={"artifact": ref, "handoff_id": h.handoff_id})
        return rec

    def on_handoff_status(self, tenant: str, handoff_id: str, status: str, reason: str) -> None:
        with self.db.session() as s:
            for row in s.scalars(select(N7IntakeRow).where(N7IntakeRow.tenant_id == tenant,
                                                           N7IntakeRow.handoff_id == handoff_id)):
                s.add(N7EventRow(event_id=new_id("N7E"), tenant_id=tenant, intake_id=row.intake_id,
                                 record={"status": status, "reason": reason, "at": self.clock().isoformat()}))

    def view(self, tenant: str, intake_id: str) -> dict:
        with self.db.session() as s:
            row = s.get(N7IntakeRow, intake_id)
            if row is None or row.tenant_id != tenant:
                raise N0Error(404, "NOT_FOUND", f"{intake_id} not found")
            events = [e.record for e in s.scalars(select(N7EventRow).where(N7EventRow.intake_id == intake_id)
                                                  .order_by(N7EventRow.created_at))]
            rec = row.record
        live = self.state.n5.handoff_status(tenant, rec["handoff_id"])
        blocked = [e for e in events if e["status"] == "BLOCKED"]
        status = "BLOCKED" if blocked or live["status"] == "BLOCKED" else rec["status"]
        return {"cost_record": rec, "status": status, "events": events, "n5_handoff": live}


__all__ = ["N7Service", "N7CostRecord", "N7_SCHEMAS", "N7_APPEND_ONLY_TABLES"]
