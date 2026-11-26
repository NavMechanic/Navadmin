"""The gate evaluation engine.

One entry point, ``evaluate``. It resolves the SKU, builds a context, runs every
rule for the gate (including the blocking rules inherited from earlier gates),
assembles the published response, writes an immutable evaluation record and —
only if nothing blocking failed — applies the state transition.

The transition is the enforcement. Returning a list of problems while letting
the workflow advance anyway would make this a report, not a gate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..enums import (EvaluationStatus, GateCode, GateState, RuleSeverity, RuleStatus)
from ..models import AuditEvent, GateEvaluation, GateInstance, Part, utcnow
from ..schemas import BlockingReason, GateEvaluationResponse
from .registry import GateRule, RuleContext, RuleResult, rules_for
from . import rules as _rules  # noqa: F401 - importing registers every rule

log = logging.getLogger(__name__)

PREDECESSOR_RULE_ID = "RULE_PREDECESSOR_GATE_PASSED"


class GateError(Exception):
    """Raised when a gate or SKU cannot be resolved."""


async def _get_or_create_gate(session: AsyncSession, sku: str,
                              gate: GateCode) -> GateInstance:
    inst = (await session.execute(
        select(GateInstance).where(GateInstance.sku == sku,
                                   GateInstance.gate_code == gate.value)
    )).scalar_one_or_none()
    if inst is None:
        inst = GateInstance(sku=sku, gate_code=gate.value, state=GateState.OPEN,
                            opened_at=utcnow())
        session.add(inst)
        await session.flush()
    return inst


async def _predecessor_check(session: AsyncSession, sku: str,
                             gate: GateCode) -> RuleResult:
    """A gate cannot be passed out of order.

    Evaluating G4 while G3 is open is a legitimate thing to do — you want to
    know where you stand. Passing it is not.
    """
    order = [g for g in GateCode if g.ordinal < gate.ordinal]
    if not order:
        return RuleResult(PREDECESSOR_RULE_ID, RuleStatus.NOT_APPLICABLE,
                          f"{gate.value} is the first gate.")
    prior = order[-1]
    inst = (await session.execute(
        select(GateInstance).where(GateInstance.sku == sku,
                                   GateInstance.gate_code == prior.value)
    )).scalar_one_or_none()
    state = inst.state if inst else GateState.NOT_STARTED.value
    if state not in (GateState.PASSED, GateState.WAIVED):
        return RuleResult(
            PREDECESSOR_RULE_ID, RuleStatus.FAILED,
            f"{prior.value} is {state} for {sku}. Gates are sequential: passing "
            f"{gate.value} now would record an approval for work the previous gate has "
            "not accepted.",
            remediation=f"Evaluate and pass {prior.value} first.",
            object_refs=[prior.value],
        )
    return RuleResult(PREDECESSOR_RULE_ID, RuleStatus.PASSED,
                      f"{prior.value} is {state}.")


async def evaluate(session: AsyncSession, sku: str, gate: GateCode,
                   *, apply_transition: bool = True,
                   actor: str | None = None,
                   include_advisories: bool = True,
                   inherit: bool = True,
                   request_id: str | None = None) -> GateEvaluationResponse:
    """Evaluate one gate for one SKU and, if it passes, transition it."""
    settings = get_settings()

    part = (await session.execute(
        select(Part).where(Part.part_number == sku))).scalar_one_or_none()
    if part is None:
        raise GateError(f"Unknown SKU {sku!r}.")

    inst = await _get_or_create_gate(session, sku, gate)
    previous_state = GateState(inst.state)

    ctx = RuleContext(session=session, sku=sku, part=part, gate=gate, settings=settings)
    applicable: list[tuple[GateRule | None, RuleResult]] = []

    # Ordering matters for readability: the predecessor check first, then the
    # gate's own rules, then inherited ones.
    applicable.append((None, await _predecessor_check(session, sku, gate)))

    for r in rules_for(gate, inherit=inherit):
        try:
            result = await r.func(ctx)
        except Exception as exc:  # noqa: BLE001
            # A rule that throws must not pass the gate by accident. It fails
            # closed, loudly, naming itself.
            log.exception("rule %s raised", r.rule_id)
            result = RuleResult(
                r.rule_id, RuleStatus.FAILED,
                f"Rule raised {type(exc).__name__}: {exc}. A rule that cannot be "
                "evaluated is treated as failed — a gate must never pass because a "
                "check crashed.",
                remediation="Check the service log for the traceback.",
            )
        applicable.append((r, result))

    blocking: list[BlockingReason] = []
    advisories: list[BlockingReason] = []
    passed_artifacts: list[str] = []
    n_pass = n_fail = n_na = 0

    for gr, res in applicable:
        severity = gr.severity if gr else RuleSeverity.BLOCKING
        if res.status is RuleStatus.PASSED:
            n_pass += 1
            passed_artifacts.extend(res.evidence)
        elif res.status is RuleStatus.NOT_APPLICABLE:
            n_na += 1
        else:
            reason = BlockingReason(
                rule_id=res.rule_id,
                status=res.status,
                detail=res.detail,
                severity=severity,
                remediation=res.remediation,
                object_refs=res.object_refs,
            )
            if severity is RuleSeverity.BLOCKING and res.status is RuleStatus.FAILED:
                n_fail += 1
                blocking.append(reason)
            else:
                advisories.append(reason)

    status = EvaluationStatus.BLOCKED if blocking else EvaluationStatus.PASSED

    # ---- transition ---------------------------------------------------
    transition_applied = False
    if apply_transition:
        if status is EvaluationStatus.PASSED:
            inst.state = GateState.PASSED
            inst.closed_at = utcnow()
            inst.closed_by = actor
            transition_applied = True
        else:
            # BLOCKED is a real state, not just a response code: the next reader
            # of the gate must see that it was evaluated and refused.
            if inst.state not in (GateState.PASSED, GateState.WAIVED):
                inst.state = GateState.BLOCKED
                transition_applied = True
            if inst.opened_at is None:
                inst.opened_at = utcnow()

    payload = GateEvaluationResponse(
        gate=gate.value,
        status=status,
        evaluation_timestamp=datetime.now(timezone.utc),
        blocking_reasons=blocking,
        passed_artifacts=sorted(set(passed_artifacts)),
        sku=sku,
        rules_evaluated=len(applicable),
        rules_passed=n_pass,
        rules_failed=n_fail,
        rules_not_applicable=n_na,
        advisories=advisories if include_advisories else [],
        gate_state=GateState(inst.state),
        previous_gate_state=previous_state,
        transition_applied=transition_applied,
        evaluated_by=actor,
    )

    record = GateEvaluation(
        gate_instance_id=inst.id,
        evaluated_by=actor,
        status=status.value,
        rules_evaluated=len(applicable),
        rules_failed=n_fail,
        transition_applied=transition_applied,
        payload=payload.model_dump(mode="json"),
    )
    session.add(record)
    session.add(AuditEvent(
        actor=actor, action="gate.evaluate", object_type="GATE",
        object_ref=f"{sku}/{gate.value}", request_id=request_id,
        detail={"status": status.value, "rules_failed": n_fail,
                "from_state": previous_state.value, "to_state": inst.state,
                "blocking_rule_ids": [b.rule_id for b in blocking]},
    ))
    await session.commit()
    await session.refresh(record)

    payload.evaluation_id = record.id
    return payload


async def waive(session: AsyncSession, sku: str, gate: GateCode, reason: str,
                rule_ids: list[str], actor: str,
                request_id: str | None = None) -> GateEvaluationResponse:
    """Pass a gate over named failing rules, on the record.

    A waiver is not a bypass: the failing rules are re-evaluated, the exact set
    being overridden is named, and any *other* failure still blocks. Waiving a
    rule that is not failing is rejected, because a pre-emptive waiver is how a
    real failure gets hidden later.
    """
    result = await evaluate(session, sku, gate, apply_transition=False, actor=actor,
                            request_id=request_id)
    failing = {b.rule_id for b in result.blocking_reasons}
    unknown = [r for r in rule_ids if r not in failing]
    if unknown:
        raise GateError(
            f"Cannot waive {', '.join(unknown)}: not currently failing for {sku} at "
            f"{gate.value}. Currently failing: {', '.join(sorted(failing)) or 'nothing'}."
        )
    remaining = [b for b in result.blocking_reasons if b.rule_id not in set(rule_ids)]
    if remaining:
        raise GateError(
            f"{len(remaining)} rule(s) would still block after this waiver: "
            + ", ".join(b.rule_id for b in remaining)
            + ". Waive all of them explicitly or fix them."
        )

    inst = await _get_or_create_gate(session, sku, gate)
    inst.state = GateState.WAIVED
    inst.closed_at = utcnow()
    inst.closed_by = actor
    inst.waived_by = actor
    inst.waiver_reason = reason

    result.gate_state = GateState.WAIVED
    result.transition_applied = True
    result.status = EvaluationStatus.PASSED

    session.add(GateEvaluation(
        gate_instance_id=inst.id, evaluated_by=actor, status="WAIVED",
        rules_evaluated=result.rules_evaluated, rules_failed=result.rules_failed,
        transition_applied=True,
        payload={**result.model_dump(mode="json"),
                 "waived_rule_ids": rule_ids, "waiver_reason": reason},
    ))
    session.add(AuditEvent(
        actor=actor, action="gate.waive", object_type="GATE",
        object_ref=f"{sku}/{gate.value}", request_id=request_id,
        detail={"waived_rule_ids": rule_ids, "reason": reason},
    ))
    await session.commit()
    return result
