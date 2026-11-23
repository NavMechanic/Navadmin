"""Orchestration, product-node (N1–N8) and vendor endpoints."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.extraction import extract_rfq
from ..core.errors import ValidationFailed
from ..core.security import WRITER_ROLES, Principal, current_principal, require_roles
from ..db import get_db, utcnow
from ..models import Bid, Vendor, WorkflowRun
from ..nodes import n1_market, n2_product, n3_design, n4_quality, n5_sourcing, n6_commercial, n7_finance, n8_lifecycle
from ..orchestration.engine import DEFINITIONS, Engine, run_view
from ..thread import service as thread
from ..vendors import service as vendors

router = APIRouter()


# --- workflows ---------------------------------------------------------------

@router.get("/workflows/definitions", tags=["orchestration"])
def definitions(p: Principal = Depends(current_principal)):
    return [{"name": d.name, "description": d.description,
             "steps": [{"name": s.name, "kind": s.kind, "depends_on": list(s.depends_on),
                        "approver_roles": list(s.approver_roles)} for s in d.steps]}
            for d in DEFINITIONS.values() if not d.name.startswith("selftest.")]


class StartIn(BaseModel):
    definition: str
    input: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(None, max_length=200)
    advance: bool = True


@router.post("/workflows", tags=["orchestration"], status_code=201)
def start_workflow(body: StartIn, request: Request, p: Principal = Depends(require_roles(*WRITER_ROLES)),
                   db: Session = Depends(get_db)):
    if body.definition.startswith("selftest."):
        raise ValidationFailed("self-test workflows run only inside the gate check")
    eng = Engine(db, request.app.state.settings)
    run = eng.start(p.tenant_id, p.username, body.definition, body.input, body.idempotency_key)
    if body.advance:
        run = eng.advance(p.tenant_id, run.id)
    return run_view(run)


@router.get("/workflows", tags=["orchestration"])
def list_runs(status: str | None = None, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    q = select(WorkflowRun).where(WorkflowRun.tenant_id == p.tenant_id)
    if status:
        q = q.where(WorkflowRun.status == status)
    return [{"id": r.id, "definition": r.definition, "status": r.status, "created_by": r.created_by}
            for r in db.scalars(q.order_by(WorkflowRun.id.desc()).limit(200)).all()]


@router.get("/workflows/{run_id}", tags=["orchestration"])
def get_run(run_id: int, request: Request, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return run_view(Engine(db, request.app.state.settings).get(p.tenant_id, run_id))


@router.post("/workflows/{run_id}/advance", tags=["orchestration"])
def advance(run_id: int, request: Request, p: Principal = Depends(require_roles(*WRITER_ROLES)), db: Session = Depends(get_db)):
    return run_view(Engine(db, request.app.state.settings).advance(p.tenant_id, run_id))


class DecisionIn(BaseModel):
    approve: bool
    notes: str = ""


@router.post("/workflows/{run_id}/steps/{step}/decision", tags=["orchestration"])
def decide(run_id: int, step: str, body: DecisionIn, request: Request, p: Principal = Depends(current_principal),
           db: Session = Depends(get_db)):
    return run_view(Engine(db, request.app.state.settings).decide(p.tenant_id, run_id, step, p, body.approve, body.notes))


# --- N1 market ---------------------------------------------------------------

class MarketIn(BaseModel):
    key: str
    opportunities: list[dict]


@router.post("/nodes/n1/market", tags=["N1 market"], status_code=201)
def n1_market_evaluate(body: MarketIn, p: Principal = Depends(require_roles("analyst", "founder", "programme_manager")),
                       db: Session = Depends(get_db)):
    result = n1_market.evaluate_opportunities(body.opportunities)
    rev = thread.create_revision(db, p.tenant_id, p.username, "MARKET", body.key, result)
    db.commit()
    return {"revision_id": rev.id, **result}


# --- N2 product ----------------------------------------------------------------

class PrdPreviewIn(BaseModel):
    text: str = Field(min_length=1)


@router.post("/nodes/n2/prd/preview", tags=["N2 product"])
def n2_preview(body: PrdPreviewIn, p: Principal = Depends(current_principal)):
    extraction = extract_rfq(body.text)
    return {"extraction": extraction, "prd": n2_product.build_prd(extraction)}


# --- N3 design -------------------------------------------------------------------

@router.post("/nodes/n3/review", tags=["N3 design"])
def n3_review(pack: dict, p: Principal = Depends(current_principal)):
    return n3_design.review_design_pack(pack)


class JunctionIn(BaseModel):
    power_w: float = Field(ge=0)
    theta_ja_c_per_w: float = Field(gt=0)
    ambient_c: float


@router.post("/nodes/n3/junction-temperature", tags=["N3 design"])
def n3_junction(body: JunctionIn, p: Principal = Depends(current_principal)):
    return {"tj_c": n3_design.junction_temperature(body.power_w, body.theta_ja_c_per_w, body.ambient_c)}


# --- N4 quality ------------------------------------------------------------------

class QualityIn(BaseModel):
    prd_revision_id: int
    tests: list[dict] | None = None
    pfmea: list[dict] = Field(default_factory=list)


@router.post("/nodes/n4/quality-gate", tags=["N4 quality"])
def n4_gate(body: QualityIn, request: Request, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    prd = thread.get_revision(db, p.tenant_id, body.prd_revision_id)
    if prd.artifact.kind != "PRD":
        raise ValidationFailed("prd_revision_id must reference a PRD revision")
    plan = n4_quality.build_test_plan(prd.content, body.tests)
    rows = n4_quality.pfmea(body.pfmea)
    return {"test_plan": plan, "pfmea": rows,
            "gate": n4_quality.quality_gate(prd.content, plan, rows, request.app.state.settings.pfmea_rpn_threshold)}


# --- N5 sourcing -----------------------------------------------------------------

class SourcingIn(BaseModel):
    bom: list[dict]
    bids: list[dict]
    currency: str = "USD"
    as_of: date | None = None
    build_qty: int = Field(gt=0)
    single_source_justification: str | None = None


@router.post("/nodes/n5/sourcing", tags=["N5 sourcing"])
def n5_sourcing_eval(body: SourcingIn, p: Principal = Depends(current_principal)):
    return n5_sourcing.run_sourcing(body.bom, body.bids, body.currency, body.as_of or utcnow().date(), body.build_qty,
                                    body.single_source_justification)


# --- N6 commercial ---------------------------------------------------------------

class BidIn(BaseModel):
    rfq_ref: str
    bidder: str
    scope: Literal["single_node", "bundle", "full_chain"]
    nodes: list[str]
    amount: str
    currency: str = "USD"
    lead_time_days: int = Field(ge=0)


@router.post("/nodes/n6/bids", tags=["N6 commercial"], status_code=201)
def n6_submit(body: BidIn, p: Principal = Depends(require_roles("vendor_lead", "procurement", "programme_manager")),
              db: Session = Depends(get_db)):
    bid = n6_commercial.submit_bid(db, p, body.model_dump())
    db.commit()
    return n6_commercial.bid_view(bid)


@router.get("/nodes/n6/bids", tags=["N6 commercial"])
def n6_list(rfq_ref: str | None = None, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    q = select(Bid).where(Bid.tenant_id == p.tenant_id)
    if rfq_ref:
        q = q.where(Bid.rfq_ref == rfq_ref)
    return [n6_commercial.bid_view(b) for b in db.scalars(q.order_by(Bid.id)).all()]


@router.post("/nodes/n6/bids/{bid_id}/shortlist", tags=["N6 commercial"])
def n6_shortlist(bid_id: int, p: Principal = Depends(require_roles("procurement", "programme_manager", "founder")),
                 db: Session = Depends(get_db)):
    bid = n6_commercial.shortlist(db, p, bid_id)
    db.commit()
    return n6_commercial.bid_view(bid)


@router.post("/nodes/n6/bids/{bid_id}/reject", tags=["N6 commercial"])
def n6_reject(bid_id: int, p: Principal = Depends(require_roles("procurement", "programme_manager", "founder")),
              db: Session = Depends(get_db)):
    bid = n6_commercial.reject(db, p, bid_id)
    db.commit()
    return n6_commercial.bid_view(bid)


class AwardIn(BaseModel):
    confirm: bool = False


@router.post("/nodes/n6/bids/{bid_id}/award", tags=["N6 commercial"])
def n6_award(bid_id: int, body: AwardIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    bid = n6_commercial.award(db, p, bid_id, body.confirm)
    db.commit()
    return n6_commercial.bid_view(bid)


# --- N7 finance ------------------------------------------------------------------

@router.post("/nodes/n7/costing", tags=["N7 finance"])
def n7_costing(inputs: dict, request: Request, p: Principal = Depends(current_principal)):
    return n7_finance.cost_model(inputs, request.app.state.settings.gst_rate)


# --- N8 lifecycle ----------------------------------------------------------------

class EventsIn(BaseModel):
    events: list[dict] = Field(min_length=1, max_length=10_000)


@router.post("/nodes/n8/events", tags=["N8 lifecycle"], status_code=201)
def n8_events(body: EventsIn, p: Principal = Depends(require_roles("engineer", "qa")), db: Session = Depends(get_db)):
    rows = n8_lifecycle.record_events(db, p, body.events)
    db.commit()
    return {"recorded": len(rows)}


class ReplayIn(BaseModel):
    units: int = Field(100, ge=1, le=10_000)
    seed: int = 42


@router.post("/nodes/n8/replay", tags=["N8 lifecycle"], status_code=201)
def n8_replay(body: ReplayIn, p: Principal = Depends(require_roles("engineer", "qa")), db: Session = Depends(get_db)):
    n = n8_lifecycle.replay_synthetic(db, p, body.units, body.seed)
    db.commit()
    return {"synthetic_events_recorded": n, "label": "SYNTHETIC REPLAY — not measured data"}


@router.get("/nodes/n8/summary", tags=["N8 lifecycle"])
def n8_summary(include_synthetic: bool = False, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return n8_lifecycle.summary(db, p.tenant_id, include_synthetic)


# --- vendors (L, T) ----------------------------------------------------------------

class VendorIn(BaseModel):
    name: str
    capability: str = "general"
    mode: Literal["api", "file_handoff"]
    endpoint: str = ""
    handoff_format: str = ""
    license_verified: bool = False
    data_terms_accepted: bool = False


@router.post("/vendors", tags=["vendors"], status_code=201)
def create_vendor(body: VendorIn, p: Principal = Depends(require_roles("vendor_lead", "programme_manager")),
                  db: Session = Depends(get_db)):
    v = vendors.create_vendor(db, p, body.model_dump())
    db.commit()
    return vendors.vendor_view(v)


@router.get("/vendors", tags=["vendors"])
def list_vendors(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return [vendors.vendor_view(v) for v in db.scalars(select(Vendor).where(Vendor.tenant_id == p.tenant_id)
                                                       .order_by(Vendor.id)).all()]


@router.post("/vendors/{vendor_id}/qualify", tags=["vendors"])
def qualify_vendor(vendor_id: int, p: Principal = Depends(require_roles("vendor_lead", "programme_manager")),
                   db: Session = Depends(get_db)):
    v = vendors.qualify(db, p, vendor_id)
    db.commit()
    return vendors.vendor_view(v)


class ScorecardIn(BaseModel):
    decision: Literal["accept", "reject"]
    criteria: dict[str, bool]
    evidence: list[str]
    notes: str = ""


@router.post("/vendors/{vendor_id}/scorecard", tags=["vendors"])
def scorecard(vendor_id: int, body: ScorecardIn, p: Principal = Depends(require_roles("qa", "vendor_lead")),
              db: Session = Depends(get_db)):
    v = vendors.record_scorecard(db, p, vendor_id, body.decision, body.criteria, body.evidence, body.notes)
    db.commit()
    return vendors.vendor_view(v)


class HandoffIn(BaseModel):
    job: dict
    idempotency_key: str


@router.post("/vendors/{vendor_id}/handoff", tags=["vendors"])
def handoff(vendor_id: int, body: HandoffIn, request: Request,
            p: Principal = Depends(require_roles("vendor_lead", "engineer")), db: Session = Depends(get_db)):
    result = vendors.handoff(db, p, request.app.state.settings, vendor_id, body.job, body.idempotency_key)
    db.commit()
    return result
