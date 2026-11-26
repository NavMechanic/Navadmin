"""N6 intake of N5 sourcing handoffs as draft bidding inputs (append-only tables).

Only the slice N5 hands off to is implemented: a released, hash-verified, live SOURCING_HANDOFF
becomes an ``N6BidInput`` draft (indicative unit material cost, supplier risk flags, lead-time
constraints). When N5 blocks the handoff (rights revoked, BOM / PRD revised) the draft is marked
BLOCKED immediately (listener) and on every read (live check).
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
from ..repository import Repository, dump, new_id
from ..security import Principal


class N6IntakeRow(Base):
    __tablename__ = "n6_intakes"
    intake_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    handoff_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class N6EventRow(Base):
    __tablename__ = "n6_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    intake_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


N6_APPEND_ONLY_TABLES = ("n6_intakes", "n6_events")
for _name in N6_APPEND_ONLY_TABLES:
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(Base.metadata.tables[_name], "after_create", DDL(_stmt).execute_if(dialect=_dialect))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HandoffIntakeRequest(_Strict):
    artifact_id: str
    revision: str


class N6BidInput(_Strict):
    model_config = ConfigDict(extra="forbid", title="N6BidInput")
    intake_id: str
    tenant_id: str
    artifact: str
    handoff_id: str
    prd_ref: str
    bom_ref: str
    design_revision: str
    build_quantity: int
    currency: str
    indicative_unit_material_cost: str
    indicative_total: str
    label: str
    supplier_risk_flags: list[dict[str, Any]]
    lead_time_constraints: dict[str, Any]
    design_verification: str
    verification_ref: Optional[str] = None
    synthetic: bool
    status: Literal["DRAFT"] = "DRAFT"
    ingested_by: str
    ingested_at: datetime


N6_SCHEMAS = {"n6_bid_input": N6BidInput}


class N6Service:
    def __init__(self, state) -> None:
        self.state, self.db, self.clock = state, state.db, state.clock
        state.n5.handoff_listeners.append(self.on_handoff_status)

    def ingest(self, p: Principal, req: HandoffIntakeRequest) -> N6BidInput:
        t = p.tenant_id
        payload, h = verify_released_handoff(self.state, p, req.artifact_id, req.revision, "N6")
        ref = f"{req.artifact_id}@{req.revision}"
        with self.db.session() as s:
            if s.scalars(select(N6IntakeRow).where(N6IntakeRow.tenant_id == t, N6IntakeRow.artifact_ref == ref)).first():
                raise N0Error(409, "ALREADY_INGESTED", f"{ref} is already an N6 bid input")
            c = payload.get("commercial") or {}
            bi = c.get("bidding_inputs") or {}
            rec = N6BidInput(intake_id=new_id("N6I"), tenant_id=t, artifact=ref, handoff_id=h.handoff_id,
                             prd_ref=payload["prd_ref"], bom_ref=payload["bom_ref"],
                             design_revision=payload["design_revision"], build_quantity=payload["build_quantity"],
                             currency=payload["base_currency"],
                             indicative_unit_material_cost=str(bi.get("indicative_unit_material_cost")),
                             indicative_total=str(payload["indicative_total"]), label=payload["indicative_scope"],
                             supplier_risk_flags=c.get("supplier_risk_flags") or [],
                             lead_time_constraints=c.get("lead_time_constraints") or {},
                             design_verification=c.get("design_verification") or "NOT_VERIFIED",
                             verification_ref=payload.get("verification_ref"), synthetic=payload["_synthetic"],
                             ingested_by=p.principal_id, ingested_at=self.clock())
            s.add(N6IntakeRow(intake_id=rec.intake_id, tenant_id=t, artifact_ref=ref, handoff_id=h.handoff_id,
                              record=dump(rec)))
            repo = Repository(s)
            repo.add_edge(t, f"artifact:{ref}", f"n6_bid_input:{rec.intake_id}", "INGESTED_BY", node="N6")
            repo.append_audit(tenant_id=t, event_type="N6_BID_INPUT_INGESTED", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"n6_bid_input:{rec.intake_id}",
                              details={"artifact": ref, "handoff_id": h.handoff_id})
        return rec

    def on_handoff_status(self, tenant: str, handoff_id: str, status: str, reason: str) -> None:
        with self.db.session() as s:
            for row in s.scalars(select(N6IntakeRow).where(N6IntakeRow.tenant_id == tenant,
                                                           N6IntakeRow.handoff_id == handoff_id)):
                s.add(N6EventRow(event_id=new_id("N6E"), tenant_id=tenant, intake_id=row.intake_id,
                                 record={"status": status, "reason": reason, "at": self.clock().isoformat()}))

    def view(self, tenant: str, intake_id: str) -> dict:
        with self.db.session() as s:
            row = s.get(N6IntakeRow, intake_id)
            if row is None or row.tenant_id != tenant:
                raise N0Error(404, "NOT_FOUND", f"{intake_id} not found")
            events = [e.record for e in s.scalars(select(N6EventRow).where(N6EventRow.intake_id == intake_id)
                                                  .order_by(N6EventRow.created_at))]
            rec = row.record
        live = self.state.n5.handoff_status(tenant, rec["handoff_id"])
        blocked = [e for e in events if e["status"] == "BLOCKED"]
        status = "BLOCKED" if blocked or live["status"] == "BLOCKED" else rec["status"]
        return {"bid_input": rec, "status": status, "events": events, "n5_handoff": live}


__all__ = ["N6Service", "N6BidInput", "HandoffIntakeRequest", "N6_SCHEMAS", "N6_APPEND_ONLY_TABLES"]
