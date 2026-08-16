"""Stage-gate endpoints — the specification's ``/plm/gates/{gate_id}/evaluate``.

Status-code contract:

* **200** — the gate passed. Body is the evaluation.
* **422** — the gate is blocked. Body is *the same schema*, with
  ``blocking_reasons`` populated, and the workflow state transition has been
  refused.

Returning the full evaluation on 422 rather than a bare error is deliberate: the
caller asked "may I proceed", and "no, and here is exactly why" is the useful
answer. ``response_model`` is therefore not declared on the route (FastAPI would
apply it only to the 200 path); the schema is enforced by constructing the model
in the engine and documented through ``responses``.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..enums import EvaluationStatus, GateCode, Role, RuleSeverity, RuleStatus
from ..models import GateEvaluation, GateInstance
from ..schemas import (BlockingReason, GateEvaluateRequest, GateEvaluationResponse,
                       GateOut, GateRuleInfo, GateWaiverRequest)
from ..security import CurrentUser, RequireGatekeeper
from ..gates import engine as gate_engine
from ..gates.registry import all_rules, rules_for

log = logging.getLogger(__name__)
router = APIRouter(prefix="/plm", tags=["stage-gate"])
Session = Annotated[AsyncSession, Depends(get_session)]

_EVAL_RESPONSES = {
    200: {"model": GateEvaluationResponse, "description": "Gate passed."},
    422: {"model": GateEvaluationResponse,
          "description": "Gate blocked. Same schema; blocking_reasons is populated and "
                         "the workflow state transition was refused."},
    404: {"description": "Unknown gate_id or SKU."},
}


def parse_gate(gate_id: str) -> GateCode:
    """Accept ``G2_DESIGN_EVT``, ``g2_design_evt`` or plain ``G2``."""
    key = gate_id.strip().upper()
    try:
        return GateCode(key)
    except ValueError:
        pass
    matches = [g for g in GateCode if g.value.startswith(f"{key}_") or g.name == key]
    if len(matches) == 1:
        return matches[0]
    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail=f"Unknown gate_id {gate_id!r}. Valid: "
               + ", ".join(g.value for g in GateCode),
    )


@router.post("/gates/{gate_id}/evaluate", responses=_EVAL_RESPONSES,
             summary="Evaluate a gate and, if it passes, transition it")
async def evaluate_gate(gate_id: str, body: GateEvaluateRequest, session: Session,
                        principal: CurrentUser, request: Request) -> JSONResponse:
    """Run every rule for the gate against the SKU.

    Any caller may evaluate — knowing where you stand should not need a
    permission. Only a gatekeeper may *transition*: a non-gatekeeper's request
    is downgraded to a dry run and the response says so.
    """
    gate = parse_gate(gate_id)
    may_transition = principal.has(Role.GATEKEEPER)
    apply = body.apply_transition and may_transition

    try:
        result = await gate_engine.evaluate(
            session, body.sku, gate, apply_transition=apply,
            actor=principal.username, include_advisories=body.include_advisories,
            request_id=getattr(request.state, "request_id", None))
    except gate_engine.GateError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if body.apply_transition and not may_transition:
        result.advisories.append(BlockingReason(
            rule_id="RULE_TRANSITION_PERMISSION",
            status=RuleStatus.WARNING,
            detail=(f"{principal.username} is not a gatekeeper, so this ran as a dry "
                    "run and the gate state was not changed."),
            severity=RuleSeverity.ADVISORY,
            remediation="Ask a gatekeeper to run the transition.",
        ))

    code = (status.HTTP_200_OK if result.status is EvaluationStatus.PASSED
            else status.HTTP_422_UNPROCESSABLE_ENTITY)
    return JSONResponse(status_code=code, content=result.model_dump(mode="json"))


@router.post("/gates/{gate_id}/waive", responses=_EVAL_RESPONSES,
             summary="Pass a gate over named failing rules, on the record")
async def waive_gate(gate_id: str, body: GateWaiverRequest, session: Session,
                     principal: RequireGatekeeper, request: Request) -> JSONResponse:
    gate = parse_gate(gate_id)
    try:
        result = await gate_engine.waive(
            session, body.sku, gate, body.reason, body.rule_ids,
            actor=principal.username,
            request_id=getattr(request.state, "request_id", None))
    except gate_engine.GateError as exc:
        # A waiver naming a rule that is not failing, or leaving other rules
        # failing, is a conflict with system state rather than a bad request.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return JSONResponse(status_code=status.HTTP_200_OK,
                        content=result.model_dump(mode="json"))


@router.get("/gates", response_model=list[GateOut], summary="Gate states")
async def list_gates(session: Session, _: CurrentUser,
                     sku: Annotated[str | None, Query()] = None) -> list[GateInstance]:
    stmt = select(GateInstance)
    if sku:
        stmt = stmt.where(GateInstance.sku == sku)
    return list((await session.execute(
        stmt.order_by(GateInstance.sku, GateInstance.gate_code))).scalars().all())


@router.get("/gates/{gate_id}/rules", response_model=list[GateRuleInfo],
            summary="The rules a gate applies, including inherited ones")
async def gate_rules(gate_id: str, _: CurrentUser,
                     inherit: Annotated[bool, Query()] = True) -> list[GateRuleInfo]:
    gate = parse_gate(gate_id)
    return [GateRuleInfo(rule_id=r.rule_id, gate=r.gate.value, title=r.title,
                         severity=r.severity, applies_to=r.applies_to,
                         standard_ref=r.standard_ref)
            for r in rules_for(gate, inherit=inherit)]


@router.get("/rules", response_model=list[GateRuleInfo],
            summary="Every registered rule across every gate")
async def list_rules(_: CurrentUser) -> list[GateRuleInfo]:
    return [GateRuleInfo(rule_id=r.rule_id, gate=r.gate.value, title=r.title,
                         severity=r.severity, applies_to=r.applies_to,
                         standard_ref=r.standard_ref)
            for r in all_rules()]


@router.get("/gates/{gate_id}/evaluations", response_model=list[dict],
            summary="The immutable evaluation history for a gate")
async def evaluations(gate_id: str, session: Session, _: CurrentUser,
                      sku: Annotated[str, Query()],
                      limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[dict]:
    gate = parse_gate(gate_id)
    inst = (await session.execute(
        select(GateInstance).where(GateInstance.sku == sku,
                                   GateInstance.gate_code == gate.value))
    ).scalar_one_or_none()
    if inst is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"{sku} has no {gate.value} gate instance yet.")
    rows = (await session.execute(
        select(GateEvaluation).where(GateEvaluation.gate_instance_id == inst.id)
        .order_by(GateEvaluation.id.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "evaluated_at": r.evaluated_at.isoformat().replace("+00:00", "Z"),
             "evaluated_by": r.evaluated_by, "status": r.status,
             "rules_evaluated": r.rules_evaluated, "rules_failed": r.rules_failed,
             "transition_applied": r.transition_applied, "payload": r.payload}
            for r in rows]
