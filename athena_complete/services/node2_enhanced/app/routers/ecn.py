"""ECN / ECO workflow endpoints."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..enums import ImpactState
from ..models import ECN, ECNAffectedItem, ECNImpact, Part
from ..schemas import (ECNCreate, ECNDecision, ECNImpactOut, ECNOut, ImpactReviewRequest)
from ..security import CurrentUser, RequireChangeBoard, RequireEngineer, RequireQuality
from ..services import ecn as ecn_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ecn", tags=["engineering-change"])
Session = Annotated[AsyncSession, Depends(get_session)]


async def _to_out(session: AsyncSession, ecn: ECN) -> ECNOut:
    items = (await session.execute(
        select(ECNAffectedItem, Part).join(Part, Part.id == ECNAffectedItem.part_id)
        .where(ECNAffectedItem.ecn_id == ecn.id))).all()
    impacts = (await session.execute(
        select(ECNImpact).where(ECNImpact.ecn_id == ecn.id)
        .order_by(ECNImpact.object_type, ECNImpact.object_ref))).scalars().all()
    return ECNOut(
        **{k: getattr(ecn, k) for k in
           ("id", "ecn_number", "title", "reason", "category", "state", "primary_sku",
            "raised_by", "raised_at", "approved_by", "effectivity_type",
            "effectivity_date", "target_gate", "impact_analysed_at")},
        affected_part_numbers=[p.part_number for _i, p in items],
        impacts=[ECNImpactOut.model_validate(i) for i in impacts],
        impact_summary=await ecn_svc.summarise(session, ecn),
    )


async def _get(session: AsyncSession, ecn_ref: str) -> ECN:
    stmt = select(ECN)
    stmt = (stmt.where(ECN.id == int(ecn_ref)) if ecn_ref.isdigit()
            else stmt.where(ECN.ecn_number == ecn_ref))
    ecn = (await session.execute(stmt)).scalar_one_or_none()
    if ecn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown ECN {ecn_ref!r}.")
    return ecn


@router.post("", response_model=ECNOut, status_code=status.HTTP_201_CREATED,
             summary="Raise an ECN and run the where-used impact analysis")
async def create(body: ECNCreate, session: Session, principal: RequireEngineer,
                 request: Request) -> ECNOut:
    """Creating a change also finds what it breaks.

    The impact analysis walks the BOM upward from every affected part and flags
    the DFMEA rows, PFMEA rows, control-plan items, design parameters,
    certificates and released documents that must be re-reviewed.
    """
    try:
        ecn = await ecn_svc.create_ecn(
            session,
            title=body.title, reason=body.reason, category=body.category,
            raised_by=principal.username,
            affected=[i.model_dump() for i in body.affected_items],
            primary_sku=body.primary_sku, effectivity_type=body.effectivity_type,
            effectivity_date=body.effectivity_date,
            effectivity_serial=body.effectivity_serial,
            stock_disposition=body.stock_disposition,
            target_gate=body.target_gate.value if body.target_gate else None,
            analyse=body.analyse_impact,
            request_id=getattr(request.state, "request_id", None))
    except ecn_svc.ECNError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return await _to_out(session, ecn)


@router.get("", response_model=list[ECNOut], summary="List ECNs")
async def list_ecns(session: Session, _: CurrentUser,
                    state: Annotated[str | None, Query()] = None,
                    sku: Annotated[str | None, Query()] = None,
                    limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[ECNOut]:
    stmt = select(ECN).order_by(ECN.id.desc()).limit(limit)
    if state:
        stmt = stmt.where(ECN.state == state.upper())
    if sku:
        stmt = stmt.where(ECN.primary_sku == sku)
    rows = (await session.execute(stmt)).scalars().all()
    return [await _to_out(session, e) for e in rows]


@router.get("/{ecn_ref}", response_model=ECNOut, summary="One ECN with its impact list")
async def get_ecn(ecn_ref: str, session: Session, _: CurrentUser) -> ECNOut:
    return await _to_out(session, await _get(session, ecn_ref))


@router.post("/{ecn_ref}/impact-analysis", response_model=ECNOut,
             summary="Re-run the impact analysis")
async def reanalyse(ecn_ref: str, session: Session, principal: RequireEngineer,
                    request: Request) -> ECNOut:
    """Idempotent: completed re-reviews are preserved, stale flags are cleared."""
    ecn = await _get(session, ecn_ref)
    try:
        await ecn_svc.analyse_impact(session, ecn, actor=principal.username,
                                     request_id=getattr(request.state, "request_id", None))
    except ecn_svc.ECNError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    await session.commit()
    return await _to_out(session, ecn)


@router.get("/{ecn_ref}/impacts", response_model=list[ECNImpactOut],
            summary="Records flagged Pending Re-Review by this change")
async def impacts(ecn_ref: str, session: Session, _: CurrentUser,
                  pending_only: Annotated[bool, Query()] = False) -> list[ECNImpact]:
    ecn = await _get(session, ecn_ref)
    stmt = select(ECNImpact).where(ECNImpact.ecn_id == ecn.id)
    if pending_only:
        stmt = stmt.where(ECNImpact.state == ImpactState.PENDING_RE_REVIEW)
    return list((await session.execute(
        stmt.order_by(ECNImpact.object_type, ECNImpact.object_ref))).scalars().all())


@router.post("/{ecn_ref}/impacts/{impact_id}", response_model=ECNImpactOut,
             summary="Record the outcome of one re-review")
async def review_impact(ecn_ref: str, impact_id: int, body: ImpactReviewRequest,
                        session: Session, principal: RequireQuality,
                        request: Request) -> ECNImpact:
    await _get(session, ecn_ref)
    try:
        return await ecn_svc.review_impact(
            session, impact_id, state=body.state, actor=principal.username,
            note=body.note, request_id=getattr(request.state, "request_id", None))
    except ecn_svc.ECNError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{ecn_ref}/decision", response_model=ECNOut,
             summary="Change-board approval or rejection")
async def decide(ecn_ref: str, body: ECNDecision, session: Session,
                 principal: RequireChangeBoard, request: Request) -> ECNOut:
    """Approval is refused while any impacted record is still Pending Re-Review."""
    ecn = await _get(session, ecn_ref)
    try:
        ecn = await ecn_svc.decide(
            session, ecn, approve=body.decision == "approve", actor=principal.username,
            note=body.note, request_id=getattr(request.state, "request_id", None))
    except ecn_svc.ECNError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await _to_out(session, ecn)
