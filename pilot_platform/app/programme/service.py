"""Programme lifecycle: load the network, enforce AND-join starts, run gate reviews.

Activity status machine:

    NOT_STARTED --start--> IN_PROGRESS --submit--> IN_REVIEW --pass--> GATE_PASSED
                                ^                      |
                                +------rework------ GATE_FAILED <--fail--+

* start   : only when EVERY predecessor is GATE_PASSED (AND-join), by the owner role or a PM.
* submit  : owner/PM attaches evidence; checks run and are stored with the review.
* decide  : gate_reviewer/PM; never the submitter (segregation of duties); checks are
            re-run at decision time and a failing check blocks a "pass".
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.errors import Conflict, Forbidden, GateBlocked, NotFound, ValidationFailed
from ..core.security import Principal
from ..db import utcnow
from ..models import Activity, GateReview, Programme
from . import cpm
from .gates import CheckContext, evaluate_gate

SEED_PLAN = Path(__file__).with_name("seed_plan.json")
NOT_STARTED, IN_PROGRESS, IN_REVIEW, GATE_PASSED, GATE_FAILED = (
    "NOT_STARTED", "IN_PROGRESS", "IN_REVIEW", "GATE_PASSED", "GATE_FAILED")
REVIEWER_ROLES = ("gate_reviewer", "programme_manager")


def load_plan(db: Session, tenant_id: str, actor: str, plan: dict) -> Programme:
    acts = plan["activities"]
    nodes = [cpm.Node(a["code"], tuple(a["predecessors"]), a["optimistic"], a["most_likely"], a["pessimistic"]) for a in acts]
    try:
        cpm.compute_schedule(nodes)  # validates ids, estimates, unknown preds and cycles
    except cpm.NetworkError as exc:
        raise ValidationFailed(f"invalid network: {exc}")
    prog = Programme(tenant_id=tenant_id, name=plan.get("name", "Programme"), source=plan.get("source", ""))
    db.add(prog)
    db.flush()
    fields = ("code", "layer", "name", "predecessors", "optimistic", "most_likely", "pessimistic", "owner",
              "owner_role", "technical_execution", "output_artifact", "gate_criteria", "gate_spec",
              "planned_start_week", "planned_finish_week", "risk_note")
    for a in acts:
        db.add(Activity(programme_id=prog.id, **{f: a.get(f) for f in fields if f in a}))
    db.flush()
    audit.record(db, tenant_id, actor, "programme.loaded", "programme", prog.id, {"activities": len(acts)})
    return prog


def load_default_plan(db: Session, tenant_id: str, actor: str) -> Programme:
    return load_plan(db, tenant_id, actor, json.loads(SEED_PLAN.read_text()))


def get_programme(db: Session, tenant_id: str, programme_id: int) -> Programme:
    prog = db.get(Programme, programme_id)
    if prog is None or prog.tenant_id != tenant_id:
        raise NotFound(f"programme {programme_id} not found")
    return prog


def get_activity(prog: Programme, code: str) -> Activity:
    for a in prog.activities:
        if a.code == code:
            return a
    raise NotFound(f"activity {code} not found")


def nodes_for(prog: Programme) -> list[cpm.Node]:
    return [cpm.Node(a.code, tuple(a.predecessors), a.optimistic, a.most_likely, a.pessimistic) for a in prog.activities]


def schedule(prog: Programme, use_actuals: bool = False) -> cpm.Schedule:
    durations = {a.code: a.actual_duration_days for a in prog.activities
                 if use_actuals and a.actual_duration_days is not None}
    return cpm.compute_schedule(nodes_for(prog), durations or None)


def _require_owner(principal: Principal, act: Activity) -> None:
    if not principal.has_any(act.owner_role, "programme_manager"):
        raise Forbidden(f"activity {act.code} is owned by role '{act.owner_role}'")


def start_activity(db: Session, principal: Principal, prog: Programme, code: str) -> Activity:
    act = get_activity(prog, code)
    _require_owner(principal, act)
    if act.status != NOT_STARTED:
        raise Conflict(f"activity {code} is {act.status}, not NOT_STARTED")
    status = {a.code: a.status for a in prog.activities}
    blockers = [p for p in act.predecessors if status[p] != GATE_PASSED]
    if blockers:
        raise GateBlocked(f"AND-join not satisfied for {code}: predecessors must pass their gates first",
                          blocking_predecessors={p: status[p] for p in blockers})
    act.status, act.started_at = IN_PROGRESS, utcnow()
    audit.record(db, principal.tenant_id, principal.username, "activity.started", "activity", f"{prog.id}:{code}")
    return act


def _checks(db: Session, principal: Principal, act: Activity, evidence: dict, settings, app) -> dict:
    return evaluate_gate(act.gate_spec or {}, evidence, CheckContext(db, principal.tenant_id, settings, app))


def submit_gate(db: Session, principal: Principal, prog: Programme, code: str, evidence: dict,
                actual_duration_days: float | None, settings, app=None) -> GateReview:
    act = get_activity(prog, code)
    _require_owner(principal, act)
    if act.status != IN_PROGRESS:
        raise Conflict(f"activity {code} is {act.status}; only IN_PROGRESS activities can be submitted")
    if actual_duration_days is not None:
        if actual_duration_days <= 0:
            raise ValidationFailed("actual_duration_days must be positive")
        act.actual_duration_days = actual_duration_days
    review = GateReview(activity_id=act.id, submitted_by=principal.username, evidence=evidence,
                        check_results=_checks(db, principal, act, evidence, settings, app))
    db.add(review)
    act.status = IN_REVIEW
    db.flush()
    audit.record(db, principal.tenant_id, principal.username, "gate.submitted", "activity", f"{prog.id}:{code}",
                 {"review_id": review.id, "checks_passed": review.check_results["passed"]})
    return review


def pending_review(db: Session, act: Activity) -> GateReview:
    review = db.scalars(select(GateReview).where(GateReview.activity_id == act.id, GateReview.decision == "pending")
                        .order_by(GateReview.id.desc())).first()
    if review is None:
        raise NotFound(f"no pending gate review for {act.code}")
    return review


def decide_gate(db: Session, principal: Principal, prog: Programme, code: str, decision: str, notes: str,
                settings, app=None) -> GateReview:
    if decision not in ("pass", "fail"):
        raise ValidationFailed("decision must be 'pass' or 'fail'")
    if not principal.has_any(*REVIEWER_ROLES):
        raise Forbidden("gate decisions require gate_reviewer or programme_manager")
    act = get_activity(prog, code)
    if act.status != IN_REVIEW:
        raise Conflict(f"activity {code} is {act.status}, not IN_REVIEW")
    review = pending_review(db, act)
    if review.submitted_by == principal.username:
        raise Forbidden("segregation of duties: the submitter cannot decide their own gate")
    review.check_results = _checks(db, principal, act, review.evidence, settings, app)  # fresh, not stale
    if decision == "pass" and not review.check_results["passed"]:
        failing = [r["name"] for r in review.check_results["results"] if not r["passed"]]
        db.flush()
        raise GateBlocked(f"gate {code} cannot pass: failing checks", failing_checks=failing,
                          results=review.check_results["results"])
    review.decision = "passed" if decision == "pass" else "failed"
    review.decided_by, review.decision_notes, review.decided_at = principal.username, notes, utcnow()
    act.status = GATE_PASSED if decision == "pass" else GATE_FAILED
    if decision == "pass":
        act.finished_at = utcnow()
    audit.record(db, principal.tenant_id, principal.username, f"gate.{review.decision}", "activity",
                 f"{prog.id}:{code}", {"review_id": review.id, "notes": notes})
    return review


def rework(db: Session, principal: Principal, prog: Programme, code: str) -> Activity:
    act = get_activity(prog, code)
    _require_owner(principal, act)
    if act.status != GATE_FAILED:
        raise Conflict(f"activity {code} is {act.status}; only GATE_FAILED activities go back to rework")
    act.status = IN_PROGRESS
    audit.record(db, principal.tenant_id, principal.username, "activity.rework", "activity", f"{prog.id}:{code}")
    return act


def activity_view(act: Activity, sched: cpm.Schedule) -> dict:
    s = sched.activities[act.code]
    return {
        "code": act.code, "layer": act.layer, "name": act.name, "owner": act.owner, "owner_role": act.owner_role,
        "status": act.status, "predecessors": act.predecessors, "successors": s.successors,
        "join": ("AND(" + ", ".join(act.predecessors) + ")") if len(act.predecessors) > 1
                else (act.predecessors[0] if act.predecessors else "START"),
        "technical_execution": act.technical_execution, "output_artifact": act.output_artifact,
        "gate_criteria": act.gate_criteria, "gate_spec": act.gate_spec,
        "pert": {"o": act.optimistic, "m": act.most_likely, "p": act.pessimistic,
                 "te": round(s.duration, 4), "variance": round(s.variance, 4)},
        "es": round(s.es, 4), "ef": round(s.ef, 4), "ls": round(s.ls, 4), "lf": round(s.lf, 4),
        "total_float": round(s.total_float, 4), "free_float": round(s.free_float, 4), "critical": s.critical,
        "planned_weeks": [act.planned_start_week, act.planned_finish_week],
        "actual_duration_days": act.actual_duration_days, "risk_note": act.risk_note,
    }
