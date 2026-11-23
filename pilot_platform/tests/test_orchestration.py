from dataclasses import replace
from datetime import timedelta

import pytest

from app.core.errors import Forbidden
from app.core.security import Principal
from app.db import utcnow
from app.orchestration.engine import (Engine, PermanentStepError, StepDef, WorkflowDef, handler, register_workflow)
from app.orchestration.selftest import run_selftest

CALLS: list[str] = []


@handler("t.flaky_twice")
def _flaky_twice(ctx):
    CALLS.append(f"{ctx.step}:{ctx.attempt}")
    if ctx.attempt < 3:
        raise RuntimeError("transient")
    return {"ok": ctx.attempt}


@handler("t.ok")
def _ok(ctx):
    CALLS.append(ctx.step)
    return {"from": ctx.step, "saw": sorted(ctx.outputs)}


@handler("t.always_fails")
def _always_fails(ctx):
    raise RuntimeError("down")


@handler("t.permanent")
def _permanent(ctx):
    raise PermanentStepError("bad input")


@handler("t.undo")
def _undo(ctx):
    CALLS.append(f"undo:{ctx.step}")


register_workflow(WorkflowDef("t.retry", (StepDef("a", "t.flaky_twice", max_attempts=3), StepDef("b", "t.ok", ("a",)))))
register_workflow(WorkflowDef("t.exhaust", (
    StepDef("a", "t.ok", compensate="t.undo"), StepDef("b", "t.ok", ("a",), compensate="t.undo"),
    StepDef("c", "t.always_fails", ("b",), max_attempts=2))))
register_workflow(WorkflowDef("t.permanent", (StepDef("a", "t.permanent", max_attempts=5),)))
register_workflow(WorkflowDef("t.approval", (
    StepDef("a", "t.ok", compensate="t.undo"),
    StepDef("gate", kind="approval", depends_on=("a",), approver_roles=("founder",)),
    StepDef("b", "t.ok", ("gate",)))))


@pytest.fixture
def engine(app, db):
    CALLS.clear()
    return Engine(db, app.state.settings)


def P(name, *roles):
    return Principal(99, name, "pilot", frozenset(roles))


def test_retries_until_success_and_passes_outputs(engine):
    run = engine.start("pilot", "eng", "t.retry", {})
    engine.advance("pilot", run.id)
    assert run.status == "SUCCEEDED"
    assert CALLS == ["a:1", "a:2", "a:3", "b"]
    assert run.context["b"]["saw"] == ["a"]


def test_exhausted_retries_compensate_in_reverse(engine):
    run = engine.start("pilot", "eng", "t.exhaust", {})
    engine.advance("pilot", run.id)
    assert run.status == "FAILED" and "step c failed" in run.error
    assert CALLS[-2:] == ["undo:b", "undo:a"]
    c = next(s for s in run.steps if s.name == "c")
    assert c.attempts == 2 and c.status == "FAILED"


def test_permanent_errors_are_not_retried(engine):
    run = engine.start("pilot", "eng", "t.permanent", {})
    engine.advance("pilot", run.id)
    assert run.status == "FAILED" and run.steps[0].attempts == 1


def test_approval_rules(engine):
    run = engine.start("pilot", "initiator", "t.approval", {})
    engine.advance("pilot", run.id)
    assert run.status == "WAITING_APPROVAL" and CALLS == ["a"]
    with pytest.raises(Forbidden):
        engine.decide("pilot", run.id, "gate", P("someone", "engineer"), True)
    with pytest.raises(Forbidden, match="four-eyes"):
        engine.decide("pilot", run.id, "gate", P("initiator", "founder"), True)
    engine.decide("pilot", run.id, "gate", P("boss", "founder"), True)
    assert run.status == "SUCCEEDED" and CALLS == ["a", "b"]


def test_rejected_approval_compensates(engine):
    run = engine.start("pilot", "initiator", "t.approval", {})
    engine.advance("pilot", run.id)
    engine.decide("pilot", run.id, "gate", P("boss", "founder"), False, "not now")
    assert run.status == "FAILED" and CALLS == ["a", "undo:a"]


def test_crashed_step_is_reclaimed_after_lease(engine, db):
    run = engine.start("pilot", "eng", "t.retry", {})
    a = run.steps[0]
    a.status, a.attempts, a.started_at = "RUNNING", 2, utcnow()
    db.commit()
    engine.advance("pilot", run.id)
    assert run.status == "RUNNING" and CALLS == []  # lease not expired: leave it alone
    a.started_at = utcnow() - timedelta(seconds=engine.settings.step_lease_seconds + 1)
    db.commit()
    engine.advance("pilot", run.id)
    assert run.status == "SUCCEEDED" and "lease expired" in a.error


def test_backoff_defers_retries(app, db):
    CALLS.clear()
    eng = Engine(db, replace(app.state.settings, retry_backoff_seconds=60))
    run = eng.start("pilot", "eng", "t.retry", {})
    eng.advance("pilot", run.id)
    assert CALLS == ["a:1"] and run.steps[0].status == "PENDING" and run.steps[0].next_attempt_at > utcnow()


def test_idempotent_start_and_tenant_scoping(engine):
    first = engine.start("pilot", "eng", "t.retry", {}, idempotency_key="k1")
    assert engine.start("pilot", "eng", "t.retry", {}, idempotency_key="k1").id == first.id
    with pytest.raises(Exception, match="not found"):
        engine.get("other-tenant", first.id)


def test_engine_selftest(app):
    assert run_selftest(app.state.settings)["passed"] is True
