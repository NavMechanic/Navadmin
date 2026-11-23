"""Engine self-test behind gate I ("Resumes failures; approvals enforced").

Runs real engine code against an isolated in-memory database, so it never touches
production data and can run at any time.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from ..core import audit
from ..core.errors import Forbidden
from ..core.security import Principal
from ..db import scratch_session, utcnow
from .engine import (RUN_FAILED, RUN_SUCCEEDED, RUN_WAITING, RUNNING, SUCCEEDED, Engine, PermanentStepError,
                     StepDef, WorkflowDef, handler, register_workflow)

TENANT = "selftest"


@handler("selftest.flaky")
def _flaky(ctx):
    if ctx.attempt == 1:
        raise RuntimeError("simulated transient failure")
    return {"attempt": ctx.attempt}


@handler("selftest.ok")
def _ok(ctx):
    return {"ok": True}


@handler("selftest.fail")
def _fail(ctx):
    raise PermanentStepError("simulated business-rule failure")


@handler("selftest.compensate")
def _compensate(ctx):
    audit.record(ctx.db, ctx.tenant_id, "engine", "selftest.compensated", "workflow_run", ctx.run_id, {"step": ctx.step})


register_workflow(WorkflowDef("selftest.approval", (
    StepDef("flaky", "selftest.flaky", max_attempts=3),
    StepDef("approve", kind="approval", depends_on=("flaky",), approver_roles=("programme_manager",)),
    StepDef("finish", "selftest.ok", ("approve",)),
)))
register_workflow(WorkflowDef("selftest.linear", (StepDef("a", "selftest.ok"), StepDef("b", "selftest.ok", ("a",)))))
register_workflow(WorkflowDef("selftest.compensation", (
    StepDef("a", "selftest.ok", compensate="selftest.compensate"),
    StepDef("b", "selftest.ok", ("a",), compensate="selftest.compensate"),
    StepDef("c", "selftest.fail", ("b",)),
)))


def _step(run, name):
    return next(s for s in run.steps if s.name == name)


def run_selftest(settings) -> dict:
    settings = replace(settings, retry_backoff_seconds=0.0)
    results: dict[str, bool] = {}
    with scratch_session() as db:
        eng = Engine(db, settings)
        pm = lambda uid, name: Principal(uid, name, TENANT, frozenset({"programme_manager"}))  # noqa: E731

        run = eng.start(TENANT, "initiator", "selftest.approval", {})
        eng.advance(TENANT, run.id)
        flaky = _step(run, "flaky")
        results["retries_transient_failure"] = flaky.status == SUCCEEDED and flaky.attempts == 2
        results["parks_for_approval"] = run.status == RUN_WAITING
        for label, who in (("blocks_initiator_self_approval", pm(1, "initiator")),
                           ("blocks_unauthorised_role", Principal(2, "viewer", TENANT, frozenset({"viewer"})))):
            try:
                eng.decide(TENANT, run.id, "approve", who, True)
                results[label] = False
            except Forbidden:
                results[label] = True
        eng.decide(TENANT, run.id, "approve", pm(3, "approver"), True)
        results["completes_after_approval"] = run.status == RUN_SUCCEEDED

        crashed = eng.start(TENANT, "initiator", "selftest.linear", {})
        a = _step(crashed, "a")
        a.status, a.attempts = RUNNING, 1
        a.started_at = utcnow() - timedelta(seconds=settings.step_lease_seconds + 5)
        db.commit()
        eng.advance(TENANT, crashed.id)
        results["resumes_after_crash"] = crashed.status == RUN_SUCCEEDED and _step(crashed, "a").attempts == 2

        failing = eng.start(TENANT, "initiator", "selftest.compensation", {})
        eng.advance(TENANT, failing.id)
        comp = [e.payload["step"] for e in audit_events(db) if e.action == "selftest.compensated"]
        results["compensates_in_reverse_order"] = failing.status == RUN_FAILED and comp == ["b", "a"]
        results["no_retry_on_permanent_error"] = _step(failing, "c").attempts == 1

        first = eng.start(TENANT, "initiator", "selftest.linear", {}, idempotency_key="same-key")
        second = eng.start(TENANT, "initiator", "selftest.linear", {}, idempotency_key="same-key")
        results["idempotent_start"] = first.id == second.id
    return {"passed": all(results.values()), "results": results}


def audit_events(db):
    from sqlalchemy import select
    from ..models import AuditEvent
    return db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == TENANT).order_by(AuditEvent.seq)).all()
