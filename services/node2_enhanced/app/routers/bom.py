"""Parts, revisions and multi-level BOM structure."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..enums import LifecycleState, Role
from ..models import AuditEvent, BomLine, Part, PartRevision, utcnow
from ..schemas import (BomExplosion, BomLineCreate, BomLineOut, PartCreate, PartOut,
                       RevisionCreate, RevisionOut, WhereUsedEntry)
from ..security import CurrentUser, RequireEngineer
from ..services import bom as bom_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/bom", tags=["bom"])
Session = Annotated[AsyncSession, Depends(get_session)]


def _bom_error(exc: bom_svc.BomError) -> HTTPException:
    """Unknown part is a 404; a structural violation is a 409, not a 400.

    The request was well-formed — it conflicts with the state of the system, and
    the caller needs to know which of the two it is.
    """
    msg = str(exc)
    if msg.startswith("Unknown part_number"):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=msg)
    if "no revision" in msg or "no revisions" in msg:
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=msg)
    return HTTPException(status.HTTP_409_CONFLICT, detail=msg)


@router.get("/parts", response_model=list[PartOut], summary="List parts")
async def list_parts(session: Session, _: CurrentUser,
                     part_type: Annotated[str | None, Query()] = None,
                     sellable_only: Annotated[bool, Query()] = False,
                     q: Annotated[str | None, Query(description="substring match")] = None,
                     limit: Annotated[int, Query(ge=1, le=500)] = 200) -> list[Part]:
    stmt = select(Part)
    if part_type:
        stmt = stmt.where(Part.part_type == part_type)
    if sellable_only:
        stmt = stmt.where(Part.is_sellable.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Part.part_number.ilike(like) | Part.name.ilike(like))
    return list((await session.execute(
        stmt.order_by(Part.part_number).limit(limit))).scalars().all())


@router.post("/parts", response_model=PartOut, status_code=status.HTTP_201_CREATED,
             summary="Create a part")
async def create_part(body: PartCreate, session: Session,
                      principal: RequireEngineer, request: Request) -> Part:
    existing = (await session.execute(
        select(Part).where(Part.part_number == body.part_number))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"{body.part_number} already exists.")
    part = Part(**body.model_dump())
    session.add(part)
    session.add(AuditEvent(actor=principal.username, action="part.create",
                           object_type="PART", object_ref=body.part_number,
                           request_id=getattr(request.state, "request_id", None)))
    await session.commit()
    await session.refresh(part)
    return part


@router.get("/parts/{part_number}", response_model=PartOut, summary="One part")
async def get_part(part_number: str, session: Session, _: CurrentUser) -> Part:
    part = (await session.execute(
        select(Part).where(Part.part_number == part_number))).scalar_one_or_none()
    if part is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown part {part_number!r}.")
    return part


@router.get("/parts/{part_number}/revisions", response_model=list[RevisionOut],
            summary="Revision history")
async def list_revisions(part_number: str, session: Session,
                         _: CurrentUser) -> list[PartRevision]:
    part = await get_part(part_number, session, _)
    return list((await session.execute(
        select(PartRevision).where(PartRevision.part_id == part.id)
        .order_by(PartRevision.id))).scalars().all())


@router.post("/parts/{part_number}/revisions", response_model=RevisionOut,
             status_code=status.HTTP_201_CREATED, summary="Create a revision")
async def create_revision(part_number: str, body: RevisionCreate, session: Session,
                          principal: RequireEngineer, request: Request) -> PartRevision:
    part = (await session.execute(
        select(Part).where(Part.part_number == part_number))).scalar_one_or_none()
    if part is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown part {part_number!r}.")
    dup = (await session.execute(
        select(PartRevision).where(PartRevision.part_id == part.id,
                                   PartRevision.rev == body.rev))).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"{part_number} rev {body.rev} already exists.")
    current = (await session.execute(
        select(PartRevision).where(PartRevision.part_id == part.id)
        .order_by(PartRevision.id.desc()).limit(1))).scalar_one_or_none()
    rev = PartRevision(part_id=part.id, **body.model_dump(),
                       lifecycle_state=LifecycleState.WIP,
                       supersedes_id=current.id if current else None)
    session.add(rev)
    session.add(AuditEvent(actor=principal.username, action="revision.create",
                           object_type="REVISION", object_ref=f"{part_number}/{body.rev}",
                           request_id=getattr(request.state, "request_id", None)))
    await session.commit()
    await session.refresh(rev)
    return rev


@router.post("/parts/{part_number}/revisions/{rev}/release", response_model=RevisionOut,
             summary="Release a revision")
async def release_revision(part_number: str, rev: str, session: Session,
                           principal: RequireEngineer, request: Request) -> PartRevision:
    part, revision = await _resolve(session, part_number, rev)
    if revision.lifecycle_state == LifecycleState.RELEASED:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"{part_number} rev {rev} is already released.")
    revision.lifecycle_state = LifecycleState.RELEASED
    revision.released_at = utcnow()
    revision.released_by = principal.username
    if revision.supersedes_id:
        old = await session.get(PartRevision, revision.supersedes_id)
        if old is not None and old.lifecycle_state == LifecycleState.RELEASED:
            old.lifecycle_state = LifecycleState.SUPERSEDED
    session.add(AuditEvent(actor=principal.username, action="revision.release",
                           object_type="REVISION", object_ref=f"{part_number}/{rev}",
                           request_id=getattr(request.state, "request_id", None)))
    await session.commit()
    await session.refresh(revision)
    return revision


async def _resolve(session: AsyncSession, part_number: str,
                   rev: str) -> tuple[Part, PartRevision]:
    try:
        return await bom_svc.resolve_revision(session, part_number, rev)
    except bom_svc.BomError as exc:
        raise _bom_error(exc) from exc


@router.get("/parts/{part_number}/revisions/{rev}/lines", response_model=list[BomLineOut],
            summary="Single-level BOM for a revision")
async def list_lines(part_number: str, rev: str, session: Session,
                     _: CurrentUser) -> list[BomLineOut]:
    _part, revision = await _resolve(session, part_number, rev)
    rows = (await session.execute(
        select(BomLine, Part, PartRevision)
        .join(Part, Part.id == BomLine.child_part_id)
        .outerjoin(PartRevision, PartRevision.id == BomLine.child_revision_id)
        .where(BomLine.parent_revision_id == revision.id)
        .order_by(BomLine.find_number))).all()
    return [
        BomLineOut(id=line.id, find_number=line.find_number,
                   child_part_number=child.part_number, child_name=child.name,
                   quantity=line.quantity,
                   child_revision=crev.rev if crev else None,
                   revision_pinned=line.child_revision_id is not None,
                   reference_designator=line.reference_designator, notes=line.notes)
        for line, child, crev in rows
    ]


@router.post("/parts/{part_number}/revisions/{rev}/lines", response_model=BomLineOut,
             status_code=status.HTTP_201_CREATED, summary="Add a BOM line")
async def add_line(part_number: str, rev: str, body: BomLineCreate, session: Session,
                   principal: RequireEngineer, request: Request) -> BomLineOut:
    _part, revision = await _resolve(session, part_number, rev)
    child = (await session.execute(
        select(Part).where(Part.part_number == body.child_part_number))).scalar_one_or_none()
    if child is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"Unknown child part {body.child_part_number!r}.")
    child_rev = None
    if body.child_revision:
        _c, child_rev = await _resolve(session, body.child_part_number, body.child_revision)
    try:
        line = await bom_svc.add_line(
            session, revision, child, Decimal(str(body.quantity)),
            find_number=body.find_number, child_revision=child_rev,
            reference_designator=body.reference_designator, notes=body.notes)
    except bom_svc.BomError as exc:
        raise _bom_error(exc) from exc

    session.add(AuditEvent(actor=principal.username, action="bom.line.add",
                           object_type="BOM_LINE",
                           object_ref=f"{part_number}/{rev}->{child.part_number}",
                           request_id=getattr(request.state, "request_id", None),
                           detail={"quantity": str(body.quantity)}))
    await session.commit()
    await session.refresh(line)
    return BomLineOut(id=line.id, find_number=line.find_number,
                      child_part_number=child.part_number, child_name=child.name,
                      quantity=line.quantity,
                      child_revision=child_rev.rev if child_rev else None,
                      revision_pinned=child_rev is not None,
                      reference_designator=line.reference_designator, notes=line.notes)


@router.get("/{part_number}/explode", response_model=BomExplosion,
            summary="Multi-level BOM explosion with cost roll-up")
async def explode(part_number: str, session: Session, _: CurrentUser,
                  rev: Annotated[str | None, Query()] = None,
                  max_level: Annotated[int, Query(ge=1, le=12)] = 12) -> BomExplosion:
    try:
        return await bom_svc.explode(session, part_number, rev, max_level=max_level)
    except bom_svc.BomError as exc:
        raise _bom_error(exc) from exc


@router.get("/{part_number}/where-used", response_model=list[WhereUsedEntry],
            summary="Every assembly and SKU that consumes this part")
async def where_used(part_number: str, session: Session,
                     _: CurrentUser) -> list[WhereUsedEntry]:
    try:
        return await bom_svc.where_used(session, part_number)
    except bom_svc.BomError as exc:
        raise _bom_error(exc) from exc


@router.get("/{part_number}/cost", response_model=dict,
            summary="Cost roll-up split by supply mode")
async def cost(part_number: str, session: Session, _: CurrentUser,
               rev: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    try:
        return await bom_svc.cost_rollup(session, part_number, rev)
    except bom_svc.BomError as exc:
        raise _bom_error(exc) from exc
