"""Durable DAG workflow engine (activity I).

* Every state change is committed, so a run survives process restarts.
* Step handlers run inside a DB transaction: if one raises, its partial writes roll back.
* Retries: up to `max_attempts`, with optional exponential backoff. `PermanentStepError`
  skips the remaining attempts (deterministic failures should not be retried).
* Crash recovery: a step left RUNNING longer than the lease is re-queued on the next advance.
* Approval steps park the run in WAITING_APPROVAL until a user holding one of the
  step's approver roles decides; the run's initiator can never approve (four-eyes).
* If a step fails for good, compensations of completed steps run in reverse completion order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core import audit
from ..core.audit import jsonable
from ..core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from ..core.security import Principal
from ..db import utcnow
from ..models import WorkflowRun, WorkflowStep
from ..programme.cpm import NetworkError, topological_order

PENDING, RUNNING, SUCCEEDED, FAILED, WAITING = "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "WAITING_APPROVAL"
RUN_RUNNING, RUN_WAITING, RUN_SUCCEEDED, RUN_FAILED, RUN_COMPENSATING = (
    "RUNNING", "WAITING_APPROVAL", "SUCCEEDED", "FAILED", "COMPENSATING")
TERMINAL = {RUN_SUCCEEDED, RUN_FAILED}


class PermanentStepError(Exception):
    """Raise from a handler when retrying cannot help (bad input, failed business rule)."""


@dataclass(frozen=True)
class StepDef:
    name: str
    handler: str | None = None
    depends_on: tuple[str, ...] = ()
    kind: str = "task"
    max_attempts: int = 3
    compensate: str | None = None
    approver_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowDef:
    name: str
    steps: tuple[StepDef, ...]
    description: str = ""


@dataclass
class StepContext:
    db: Session
    settings: Any
    tenant_id: str
    run_id: int
    step: str
    attempt: int
    input: dict
    outputs: dict
    actor: str


HANDLERS: dict[str, Callable[[StepContext], dict | None]] = {}
DEFINITIONS: dict[str, WorkflowDef] = {}


def handler(name: str):
    def register(fn):
        HANDLERS[name] = fn
        return fn
    return register


def register_workflow(defn: WorkflowDef) -> WorkflowDef:
    names = [s.name for s in defn.steps]
    if len(set(names)) != len(names):
        raise ValueError(f"{defn.name}: duplicate step names")
    try:
        topological_order({s.name: s.depends_on for s in defn.steps})
    except NetworkError as exc:
        raise ValueError(f"{defn.name}: {exc}") from exc
    for s in defn.steps:
        if s.kind == "task" and not s.handler:
            raise ValueError(f"{defn.name}.{s.name}: task steps need a handler")
        if s.kind == "approval" and not s.approver_roles:
            raise ValueError(f"{defn.name}.{s.name}: approval steps need approver_roles")
    DEFINITIONS[defn.name] = defn
    return defn


@dataclass
class Engine:
    db: Session
    settings: Any

    # -- lifecycle ---------------------------------------------------------
    def start(self, tenant_id: str, actor: str, definition: str, payload: dict,
              idempotency_key: str | None = None) -> WorkflowRun:
        defn = DEFINITIONS.get(definition)
        if defn is None:
            raise NotFound(f"workflow definition {definition!r} not registered")
        if idempotency_key:
            existing = self.db.scalars(select(WorkflowRun).where(
                WorkflowRun.tenant_id == tenant_id, WorkflowRun.idempotency_key == idempotency_key)).first()
            if existing:
                return existing
        run = WorkflowRun(tenant_id=tenant_id, definition=definition, status=RUN_RUNNING, input=jsonable(payload),
                          context={}, idempotency_key=idempotency_key, created_by=actor)
        self.db.add(run)
        try:
            self.db.flush()
        except IntegrityError:  # concurrent start with the same key
            self.db.rollback()
            return self.db.scalars(select(WorkflowRun).where(
                WorkflowRun.tenant_id == tenant_id, WorkflowRun.idempotency_key == idempotency_key)).one()
        for s in defn.steps:
            self.db.add(WorkflowStep(run_id=run.id, name=s.name, kind=s.kind, handler=s.handler,
                                     compensate_handler=s.compensate, depends_on=list(s.depends_on),
                                     approver_roles=list(s.approver_roles), max_attempts=s.max_attempts))
        audit.record(self.db, tenant_id, actor, "workflow.started", "workflow_run", run.id, {"definition": definition})
        self.db.commit()
        return run

    def get(self, tenant_id: str, run_id: int) -> WorkflowRun:
        run = self.db.get(WorkflowRun, run_id)
        if run is None or run.tenant_id != tenant_id:
            raise NotFound(f"workflow run {run_id} not found")
        return run

    def advance(self, tenant_id: str, run_id: int, max_steps: int = 100) -> WorkflowRun:
        run = self.get(tenant_id, run_id)
        if run.status in TERMINAL:
            return run
        if run.status == RUN_COMPENSATING:  # crashed mid-compensation: finish it (idempotent per step)
            self._compensate(run, run.error)
            return run
        self._reclaim_stale(run)
        executed = 0
        while executed < max_steps:
            ready = self._ready_steps(run)
            if not ready:
                break
            for step in ready:
                executed += 1
                if step.kind == "approval":
                    step.status = WAITING
                    self.db.commit()
                    continue
                self._execute(run, step)
                if step.status == FAILED:
                    self._compensate(run, f"step {step.name} failed: {step.error}")
                    return run
        self._refresh_status(run)
        self.db.commit()
        return run

    def decide(self, tenant_id: str, run_id: int, step_name: str, principal: Principal, approve: bool,
               notes: str = "") -> WorkflowRun:
        run = self.get(tenant_id, run_id)
        step = next((s for s in run.steps if s.name == step_name), None)
        if step is None or step.kind != "approval":
            raise NotFound(f"approval step {step_name!r} not found in run {run_id}")
        if step.status != WAITING:
            raise Conflict(f"step {step_name} is {step.status}, not awaiting approval")
        if not principal.has_any(*step.approver_roles):
            raise Forbidden(f"approval requires one of: {', '.join(step.approver_roles)}")
        if principal.username == run.created_by:
            raise Forbidden("four-eyes rule: the run initiator cannot approve its own steps")
        step.decided_by, step.finished_at = principal.username, utcnow()
        step.output = {"approved": approve, "decided_by": principal.username, "notes": notes}
        audit.record(self.db, tenant_id, principal.username, "workflow.approval_" + ("granted" if approve else "rejected"),
                     "workflow_run", run.id, {"step": step_name, "notes": notes})
        if approve:
            step.status = SUCCEEDED
            step.completion_seq = self._next_seq(run)
            run.context = {**run.context, step.name: step.output}
            self.db.commit()
            return self.advance(tenant_id, run_id)
        step.status, step.error = FAILED, f"rejected by {principal.username}: {notes}"
        self.db.commit()
        self._compensate(run, f"approval {step_name} rejected")
        return run

    # -- internals -----------------------------------------------------------
    def _ready_steps(self, run: WorkflowRun) -> list[WorkflowStep]:
        status = {s.name: s.status for s in run.steps}
        now = utcnow()
        return [s for s in run.steps if s.status == PENDING
                and all(status[d] == SUCCEEDED for d in s.depends_on)
                and (s.next_attempt_at is None or s.next_attempt_at <= now)]

    def _reclaim_stale(self, run: WorkflowRun) -> None:
        cutoff = utcnow() - timedelta(seconds=self.settings.step_lease_seconds)
        for s in run.steps:
            if s.status == RUNNING and s.started_at is not None and s.started_at < cutoff:
                s.status, s.error = PENDING, (s.error + " | " if s.error else "") + "lease expired; re-queued"
        self.db.commit()

    def _next_seq(self, run: WorkflowRun) -> int:
        return max((s.completion_seq or 0) for s in run.steps) + 1

    def _ctx(self, run: WorkflowRun, step: WorkflowStep) -> StepContext:
        return StepContext(self.db, self.settings, run.tenant_id, run.id, step.name, step.attempts,
                           run.input, dict(run.context), run.created_by)

    def _execute(self, run: WorkflowRun, step: WorkflowStep) -> None:
        fn = HANDLERS.get(step.handler)
        step.status, step.started_at = RUNNING, utcnow()
        step.attempts += 1
        self.db.commit()  # durable "in flight" marker before side effects
        try:
            if fn is None:
                raise PermanentStepError(f"handler {step.handler!r} not registered")
            out = jsonable(fn(self._ctx(run, step)) or {})
        except Exception as exc:
            self.db.rollback()  # discard the handler's partial writes
            permanent = isinstance(exc, (PermanentStepError, ValidationFailed))
            step.error = f"{type(exc).__name__}: {exc}"[:2000]
            if permanent or step.attempts >= step.max_attempts:
                step.status, step.finished_at = FAILED, utcnow()
            else:
                step.status = PENDING
                backoff = self.settings.retry_backoff_seconds * (2 ** (step.attempts - 1))
                step.next_attempt_at = utcnow() + timedelta(seconds=backoff) if backoff else None
            self.db.commit()
            return
        step.status, step.output, step.finished_at = SUCCEEDED, out, utcnow()
        step.completion_seq = self._next_seq(run)
        run.context = {**run.context, step.name: out}
        self.db.commit()

    def _compensate(self, run: WorkflowRun, reason: str) -> None:
        run.status, run.error = RUN_COMPENSATING, reason
        self.db.commit()
        done = sorted((s for s in run.steps if s.status == SUCCEEDED and s.compensate_handler and not s.compensated),
                      key=lambda s: s.completion_seq or 0, reverse=True)
        errors = []
        for s in done:
            fn = HANDLERS.get(s.compensate_handler)
            try:
                if fn is None:
                    raise RuntimeError(f"compensation handler {s.compensate_handler!r} not registered")
                fn(self._ctx(run, s))
                s.compensated = True
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                errors.append(f"{s.name}: {exc}")
        run.status = RUN_FAILED
        if errors:
            run.error += " | compensation errors: " + "; ".join(errors)
        audit.record(self.db, run.tenant_id, "engine", "workflow.failed", "workflow_run", run.id,
                     {"reason": reason, "compensated": [s.name for s in done if s.compensated]})
        self.db.commit()

    def _refresh_status(self, run: WorkflowRun) -> None:
        statuses = {s.status for s in run.steps}
        if statuses == {SUCCEEDED}:
            if run.status != RUN_SUCCEEDED:
                audit.record(self.db, run.tenant_id, "engine", "workflow.succeeded", "workflow_run", run.id)
            run.status = RUN_SUCCEEDED
        elif WAITING in statuses:
            run.status = RUN_WAITING
        else:
            run.status = RUN_RUNNING


def run_view(run: WorkflowRun) -> dict:
    return {
        "id": run.id, "definition": run.definition, "status": run.status, "created_by": run.created_by,
        "idempotency_key": run.idempotency_key, "error": run.error, "input": run.input,
        "created_at": run.created_at.isoformat(), "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "steps": [{
            "name": s.name, "kind": s.kind, "status": s.status, "depends_on": s.depends_on, "attempts": s.attempts,
            "max_attempts": s.max_attempts, "approver_roles": s.approver_roles, "decided_by": s.decided_by,
            "compensated": s.compensated, "error": s.error, "output": s.output,
        } for s in run.steps],
    }
