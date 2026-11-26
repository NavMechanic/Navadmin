"""Programme / CPM network / gate endpoints — the workflow from the source HTML."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.security import Principal, current_principal, require_roles
from ..db import get_db
from ..models import GateReview, Programme
from ..programme import cpm, service
from ..programme.gates import CheckContext, evaluate_gate

router = APIRouter(prefix="/programmes", tags=["programme"])


def _prog(db: Session, p: Principal, pid: int) -> Programme:
    return service.get_programme(db, p.tenant_id, pid)


class PlanIn(BaseModel):
    plan: dict | None = Field(None, description="Same shape as app/programme/seed_plan.json; omit to load the pilot plan")


@router.post("", status_code=201)
def create_programme(body: PlanIn, p: Principal = Depends(require_roles("programme_manager")), db: Session = Depends(get_db)):
    prog = service.load_plan(db, p.tenant_id, p.username, body.plan) if body.plan else \
        service.load_default_plan(db, p.tenant_id, p.username)
    db.commit()
    return {"id": prog.id, "name": prog.name, "activities": len(prog.activities)}


@router.get("")
def list_programmes(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    progs = db.scalars(select(Programme).where(Programme.tenant_id == p.tenant_id).order_by(Programme.id)).all()
    return [{"id": x.id, "name": x.name, "source": x.source, "activities": len(x.activities)} for x in progs]


@router.get("/{pid}")
def get_programme(pid: int, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    prog = _prog(db, p, pid)
    sched = service.schedule(prog)
    status = {}
    for a in prog.activities:
        status[a.status] = status.get(a.status, 0) + 1
    return {"id": prog.id, "name": prog.name, "source": prog.source, "status_counts": status, **sched.summary(),
            "startable_now": [a.code for a in prog.activities if a.status == service.NOT_STARTED and all(
                next(x for x in prog.activities if x.code == pr).status == service.GATE_PASSED for pr in a.predecessors)]}


@router.get("/{pid}/schedule")
def get_schedule(pid: int, use_actuals: bool = False, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    prog = _prog(db, p, pid)
    sched = service.schedule(prog, use_actuals)
    return {**sched.summary(), "basis": "actual durations where recorded, PERT otherwise" if use_actuals else "PERT",
            "activities": [service.activity_view(a, sched) for a in prog.activities], "links": sched.links()}


@router.get("/{pid}/monte-carlo")
def monte_carlo(pid: int, iterations: int = 5000, seed: int = 7, p: Principal = Depends(current_principal),
                db: Session = Depends(get_db)):
    prog = _prog(db, p, pid)
    return {"deterministic": service.schedule(prog).summary(), "simulation": cpm.monte_carlo(service.nodes_for(prog), iterations, seed)}


@router.get("/{pid}/gate-register")
def gate_register(pid: int, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    prog = _prog(db, p, pid)
    sched = service.schedule(prog)
    keys = ("code", "layer", "name", "join", "technical_execution", "output_artifact", "gate_criteria", "gate_spec",
            "owner", "status", "pert", "total_float", "critical", "planned_weeks")
    return [{k: v for k, v in service.activity_view(a, sched).items() if k in keys} for a in prog.activities]


@router.get("/{pid}/activities/{code}")
def get_activity(pid: int, code: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    prog = _prog(db, p, pid)
    return service.activity_view(service.get_activity(prog, code), service.schedule(prog))


@router.get("/{pid}/activities/{code}/trace")
def trace(pid: int, code: str, direction: Literal["up", "down"] = "down", p: Principal = Depends(current_principal),
          db: Session = Depends(get_db)):
    prog = _prog(db, p, pid)
    service.get_activity(prog, code)
    return {"activity": code, "direction": direction, "activities": cpm.trace(service.nodes_for(prog), code, direction)}


@router.get("/{pid}/activities/{code}/reviews")
def reviews(pid: int, code: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    act = service.get_activity(_prog(db, p, pid), code)
    rows = db.scalars(select(GateReview).where(GateReview.activity_id == act.id).order_by(GateReview.id)).all()
    return [{"id": r.id, "submitted_by": r.submitted_by, "evidence": r.evidence, "checks": r.check_results,
             "decision": r.decision, "decided_by": r.decided_by, "notes": r.decision_notes} for r in rows]


@router.post("/{pid}/activities/{code}/start")
def start(pid: int, code: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    act = service.start_activity(db, p, _prog(db, p, pid), code)
    db.commit()
    return {"code": act.code, "status": act.status}


class EvidenceIn(BaseModel):
    evidence: dict = Field(default_factory=dict)
    actual_duration_days: float | None = None


@router.post("/{pid}/activities/{code}/check")
def dry_run_gate(pid: int, code: str, body: EvidenceIn, request: Request, p: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)):
    """Evaluate a gate against evidence without changing any state."""
    act = service.get_activity(_prog(db, p, pid), code)
    result = evaluate_gate(act.gate_spec or {}, body.evidence, CheckContext(db, p.tenant_id, request.app.state.settings, request.app))
    db.rollback()
    return result


@router.post("/{pid}/activities/{code}/submit")
def submit(pid: int, code: str, body: EvidenceIn, request: Request, p: Principal = Depends(current_principal),
           db: Session = Depends(get_db)):
    review = service.submit_gate(db, p, _prog(db, p, pid), code, body.evidence, body.actual_duration_days,
                                 request.app.state.settings, request.app)
    db.commit()
    return {"review_id": review.id, "status": "IN_REVIEW", "checks": review.check_results}


class DecisionIn(BaseModel):
    decision: Literal["pass", "fail"]
    notes: str = ""


@router.post("/{pid}/activities/{code}/decide")
def decide(pid: int, code: str, body: DecisionIn, request: Request, p: Principal = Depends(current_principal),
           db: Session = Depends(get_db)):
    review = service.decide_gate(db, p, _prog(db, p, pid), code, body.decision, body.notes,
                                 request.app.state.settings, request.app)
    db.commit()
    return {"review_id": review.id, "decision": review.decision, "decided_by": review.decided_by, "checks": review.check_results}


@router.post("/{pid}/activities/{code}/rework")
def rework(pid: int, code: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    act = service.rework(db, p, _prog(db, p, pid), code)
    db.commit()
    return {"code": act.code, "status": act.status}
