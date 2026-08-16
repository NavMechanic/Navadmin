"""CAPA endpoints.

This is the integration point Node 6 calls when a field-failure cluster crosses
its action threshold. The design decision that shapes the whole module:

**Field evidence proposes an FMEA occurrence re-rating. It never applies one.**

Occurrence feeds Action Priority. Action Priority is what the G2 gate rule
blocks on. If Node 6 could write `occurrence` directly it could move a row from
HIGH to MEDIUM and unblock a gate, automatically, on the strength of a
clustering run — with nobody having decided anything. Field data is exactly the
evidence that *should* drive a re-rating, and it is exactly the evidence that
must not apply one unattended.

So `POST /plm/capa` records the proposal and flags the FMEA row for re-review.
`POST /plm/capa/{ref}/rerating` applies it, needs the quality role, needs a
written reason, and records who did it and what the AP was before and after.
"""
from __future__ import annotations

import logging
from datetime import date, timezone
from typing import Annotated, Any

from fastapi import (APIRouter, Depends, HTTPException, Query, Request,
                     Response, status)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..enums import ActionPriority, CapaSource, CapaState, ReratingState, ReviewState
from ..models import AuditEvent, Capa, FmeaRow, utcnow
from ..schemas import CapaCreate, CapaDecision, CapaOut, ReratingDecision
from ..security import RequireFieldQuality, RequireQuality, RequireViewer
from ..services import action_priority as ap_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/plm/capa", tags=["capa"])
Session = Annotated[AsyncSession, Depends(get_session)]

# States from which a CAPA may still be worked.
_OPEN_STATES = {CapaState.OPEN, CapaState.CONTAINMENT, CapaState.INVESTIGATION,
                CapaState.ACTION_IMPLEMENTED, CapaState.EFFECTIVENESS_CHECK}


async def _next_number(session: AsyncSession) -> str:
    year = utcnow().year
    prefix = f"CAPA-{year}-"
    n = (await session.execute(
        select(func.count()).select_from(Capa)
        .where(Capa.capa_number.like(f"{prefix}%")))).scalar_one()
    return f"{prefix}{n + 1:03d}"


async def _to_out(session: AsyncSession, capa: Capa) -> CapaOut:
    out = CapaOut.model_validate(capa)
    notes: list[str] = []
    if capa.linked_fmea_row_id:
        row = await session.get(FmeaRow, capa.linked_fmea_row_id)
        if row is not None:
            out.linked_fmea_row_ref = row.row_ref
            out.current_occurrence = row.occurrence
            out.current_action_priority = row.action_priority
    if capa.rerating_state == ReratingState.PROPOSED:
        notes.append(
            f"An occurrence re-rating to {capa.proposed_occurrence} is PROPOSED and "
            "has not been applied. The FMEA row still carries its original rating. "
            "Applying it requires the quality role and a written reason.")
    if not ap_svc.TABLE_VERIFIED:
        notes.append(
            "Action Priority is computed from a table that has not been verified "
            "against the licensed AIAG-VDA handbook. A re-rating that would lower "
            "priority is refused while that is true.")
    out.notes = notes
    return out


async def _get(session: AsyncSession, ref: str) -> Capa:
    stmt = select(Capa)
    stmt = (stmt.where(Capa.id == int(ref)) if ref.isdigit()
            else stmt.where(Capa.capa_number == ref))
    capa = (await session.execute(stmt)).scalar_one_or_none()
    if capa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown CAPA {ref!r}.")
    return capa


@router.post("", response_model=CapaOut, status_code=status.HTTP_201_CREATED,
             summary="Open a CAPA (idempotent on source + source_ref)")
async def create(body: CapaCreate, session: Session, principal: RequireFieldQuality,
                 request: Request, response: Response) -> CapaOut:
    """Open a CAPA from evidence.

    Idempotent on ``(source, source_ref)``. Node 6 retries clustering runs, and
    without this a retry opens a second CAPA for the same cluster — which is how
    one field failure ends up investigated twice by two people who do not know
    about each other. A repeat call returns the existing CAPA with 200.
    """
    if body.source_ref:
        existing = (await session.execute(
            select(Capa).where(Capa.source == body.source,
                               Capa.source_ref == body.source_ref)
        )).scalar_one_or_none()
        if existing is not None:
            log.info("CAPA %s already exists for %s/%s — returning it",
                     existing.capa_number, body.source, body.source_ref)
            # 200, not the route's declared 201. A caller that only reads the
            # status code must be able to tell "I opened a CAPA" from "one was
            # already open", or a retry loop reports a second investigation
            # that does not exist.
            response.status_code = status.HTTP_200_OK
            out = await _to_out(session, existing)
            out.notes.insert(0, (
                f"This CAPA already existed for {body.source.value} "
                f"{body.source_ref!r}; it was returned rather than duplicated."))
            return out

    row: FmeaRow | None = None
    if body.linked_fmea_row_ref:
        row = (await session.execute(
            select(FmeaRow).where(FmeaRow.row_ref == body.linked_fmea_row_ref)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=f"Unknown FMEA row {body.linked_fmea_row_ref!r}.")
        if body.sku and row.sku != body.sku:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(f"FMEA row {row.row_ref} belongs to {row.sku}, but the CAPA "
                        f"is for {body.sku}. Re-rating a different product's row on "
                        "this evidence would be wrong in a way nobody would notice."))

    capa = Capa(
        capa_number=await _next_number(session),
        title=body.title, description=body.description,
        state=CapaState.OPEN, source=body.source, source_ref=body.source_ref,
        sku=body.sku, component_lot=body.component_lot, severity=body.severity,
        raised_by=principal.username, assigned_to=body.assigned_to,
        due_date=body.due_date,
        affected_volume_pct=body.affected_volume_pct,
        affected_unit_count=body.affected_unit_count,
        population_count=body.population_count,
        evidence=body.evidence,
        linked_fmea_row_id=row.id if row else None,
        proposed_occurrence=body.proposed_occurrence,
        occurrence_rationale=body.occurrence_rationale,
        rerating_state=(ReratingState.PROPOSED if body.proposed_occurrence is not None
                        else ReratingState.NONE),
    )
    session.add(capa)
    await session.flush()

    # Flag the row for re-review. This is the part that is applied immediately,
    # and it is safe to: PENDING_RE_REVIEW makes a gate stricter, never looser.
    if row is not None and body.proposed_occurrence is not None:
        row.review_state = ReviewState.PENDING_RE_REVIEW
        capa.occurrence_before = row.occurrence
        capa.action_priority_before = row.action_priority

    session.add(AuditEvent(
        actor=principal.username, action="capa.create", object_type="CAPA",
        object_ref=capa.capa_number,
        request_id=getattr(request.state, "request_id", None),
        detail={"source": body.source.value, "source_ref": body.source_ref,
                "sku": body.sku, "component_lot": body.component_lot,
                "affected_volume_pct": body.affected_volume_pct,
                "proposed_occurrence": body.proposed_occurrence,
                "linked_fmea_row": row.row_ref if row else None}))
    await session.commit()
    await session.refresh(capa)
    log.info("CAPA %s opened by %s (%s)", capa.capa_number, principal.username,
             body.source.value)
    return await _to_out(session, capa)


@router.get("", response_model=list[CapaOut], summary="List CAPAs")
async def listing(session: Session, principal: RequireViewer,
                  state: CapaState | None = None, sku: str | None = None,
                  source: CapaSource | None = None,
                  component_lot: str | None = None,
                  open_only: bool = False,
                  limit: int = Query(50, ge=1, le=500)) -> list[CapaOut]:
    stmt = select(Capa).order_by(Capa.raised_at.desc()).limit(limit)
    if state:
        stmt = stmt.where(Capa.state == state)
    if open_only:
        stmt = stmt.where(Capa.state.in_([s.value for s in _OPEN_STATES]))
    if sku:
        stmt = stmt.where(Capa.sku == sku)
    if source:
        stmt = stmt.where(Capa.source == source)
    if component_lot:
        stmt = stmt.where(Capa.component_lot == component_lot)
    rows = (await session.execute(stmt)).scalars().all()
    return [await _to_out(session, c) for c in rows]


@router.get("/{ref}", response_model=CapaOut, summary="One CAPA")
async def detail(ref: str, session: Session, principal: RequireViewer) -> CapaOut:
    return await _to_out(session, await _get(session, ref))


@router.post("/{ref}/state", response_model=CapaOut, summary="Move a CAPA's state")
async def transition(ref: str, body: CapaDecision, session: Session,
                     principal: RequireQuality, request: Request) -> CapaOut:
    capa = await _get(session, ref)
    before = capa.state

    if body.state in (CapaState.CLOSED, CapaState.REJECTED):
        if not body.note:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=("Closing or rejecting a CAPA requires a note. A CAPA closed "
                        "with no recorded reason is indistinguishable from one that "
                        "was forgotten about."))
        if (capa.rerating_state == ReratingState.PROPOSED
                and body.state == CapaState.CLOSED):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=(f"{capa.capa_number} carries an occurrence re-rating that is "
                        "still PROPOSED. Apply or reject it before closing — "
                        "otherwise the evidence that opened this CAPA leaves no "
                        "trace in the FMEA and the next occurrence looks like the "
                        "first."))
        capa.closed_by = principal.username
        capa.closed_at = utcnow()
        capa.closure_note = body.note

    capa.state = body.state
    session.add(AuditEvent(
        actor=principal.username, action="capa.state", object_type="CAPA",
        object_ref=capa.capa_number,
        request_id=getattr(request.state, "request_id", None),
        detail={"from": before, "to": body.state.value, "note": body.note}))
    await session.commit()
    await session.refresh(capa)
    return await _to_out(session, capa)


@router.post("/{ref}/rerating", response_model=CapaOut,
             summary="Apply or reject the proposed FMEA occurrence re-rating")
async def rerating(ref: str, body: ReratingDecision, session: Session,
                   principal: RequireQuality, request: Request) -> CapaOut:
    """Apply the proposal. This is the only path that writes an FMEA occurrence.

    It requires the quality role, which the field-quality service account that
    opened the CAPA does not hold. That is the point: the machine gathers the
    evidence and a named person decides what it means.
    """
    capa = await _get(session, ref)
    if capa.rerating_state != ReratingState.PROPOSED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(f"{capa.capa_number} has no proposed re-rating to decide "
                    f"(state {capa.rerating_state})."))
    row = await session.get(FmeaRow, capa.linked_fmea_row_id or 0)
    if row is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"{capa.capa_number} references an FMEA row that no longer exists.")

    if not body.apply:
        capa.rerating_state = ReratingState.REJECTED
        capa.occurrence_rationale = (
            f"{capa.occurrence_rationale or ''}\n\nREJECTED by {principal.username}: "
            f"{body.reason}").strip()
        row.review_state = ReviewState.CURRENT
        session.add(AuditEvent(
            actor=principal.username, action="capa.rerating_rejected",
            object_type="CAPA", object_ref=capa.capa_number,
            request_id=getattr(request.state, "request_id", None),
            detail={"fmea_row": row.row_ref, "proposed": capa.proposed_occurrence,
                    "reason": body.reason}))
        await session.commit()
        await session.refresh(capa)
        return await _to_out(session, capa)

    before_occ = row.occurrence
    before_ap = row.action_priority
    after_ap = ap_svc.action_priority(row.severity, capa.proposed_occurrence,
                                      row.detection)

    # The guard that matters. An unverified table may put a cell one band low,
    # and a row that drops from HIGH to MEDIUM stops blocking the gate. Raising
    # priority on an unverified table is safe — it can only be over-cautious.
    if (not ap_svc.TABLE_VERIFIED
            and ap_svc.rank(after_ap) < ap_svc.rank(before_ap)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"Applying occurrence {capa.proposed_occurrence} would move "
                f"{row.row_ref} from {before_ap} to {after_ap.value}, lowering its "
                "Action Priority. The AP table in this build has not been verified "
                "against the licensed AIAG-VDA handbook, and a downgrade is what "
                "stops a row blocking a gate. Verify the table "
                "(app/services/action_priority.py, TABLE_VERIFIED) or record the "
                "downgrade through a change with a named approver."))

    row.occurrence = capa.proposed_occurrence
    row.action_priority = after_ap.value
    row.review_state = ReviewState.PENDING_RE_REVIEW

    capa.rerating_state = ReratingState.APPLIED
    capa.rerating_applied_by = principal.username
    capa.rerating_applied_at = utcnow()
    capa.occurrence_before = before_occ
    capa.action_priority_before = before_ap
    capa.action_priority_after = after_ap.value

    session.add(AuditEvent(
        actor=principal.username, action="capa.rerating_applied", object_type="FMEA_ROW",
        object_ref=row.row_ref,
        request_id=getattr(request.state, "request_id", None),
        detail={"capa": capa.capa_number,
                "occurrence": {"from": before_occ, "to": row.occurrence},
                "action_priority": {"from": before_ap, "to": after_ap.value},
                "severity": row.severity, "detection": row.detection,
                "reason": body.reason,
                "table_verified": ap_svc.TABLE_VERIFIED}))
    await session.commit()
    await session.refresh(capa)
    log.info("CAPA %s: %s occurrence %d -> %d, AP %s -> %s by %s",
             capa.capa_number, row.row_ref, before_occ, row.occurrence,
             before_ap, after_ap.value, principal.username)
    return await _to_out(session, capa)


@router.get("/tools/action-priority", summary="Explain an AP cell", tags=["capa"])
async def explain_ap(severity: int = Query(ge=1, le=10),
                     occurrence: int = Query(ge=1, le=10),
                     detection: int = Query(ge=1, le=10)) -> dict[str, Any]:
    return ap_svc.explain(severity, occurrence, detection)
