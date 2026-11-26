"""FMEA, control plan, design parameters, artifacts and compliance."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..enums import ArtifactStatus, ReviewState
from ..models import (Artifact, AuditEvent, Certificate, ControlPlanItem,
                      DesignParameter, FmeaRow, Part, User, utcnow)
from ..schemas import (ArtifactCreate, ArtifactOut, CertificateOut,
                       ComplianceRefreshResponse, ControlPlanItemOut, FmeaAssignOwner,
                       FmeaRowOut)
from ..security import CurrentUser, RequireEngineer, RequireQuality
from ..services import compliance as compliance_svc

log = logging.getLogger(__name__)
router = APIRouter(tags=["records"])
Session = Annotated[AsyncSession, Depends(get_session)]


def _fmea_out(row: FmeaRow, owner: User | None) -> FmeaRowOut:
    return FmeaRowOut(**{k: getattr(row, k) for k in
                         ("id", "fmea_type", "row_ref", "sku", "item_function",
                          "failure_mode", "severity", "occurrence", "detection",
                          "action_priority", "recommended_action",
                          "responsibility_function", "target_gate", "status",
                          "review_state")},
                      owner_username=owner.username if owner else None)


@router.get("/fmea", response_model=list[FmeaRowOut], summary="FMEA rows")
async def list_fmea(session: Session, _: CurrentUser,
                    sku: Annotated[str | None, Query()] = None,
                    fmea_type: Annotated[str | None, Query()] = None,
                    action_priority: Annotated[str | None, Query()] = None,
                    unassigned_only: Annotated[bool, Query()] = False) -> list[FmeaRowOut]:
    stmt = select(FmeaRow, User).outerjoin(User, User.id == FmeaRow.owner_user_id)
    if sku:
        stmt = stmt.where(FmeaRow.sku == sku)
    if fmea_type:
        stmt = stmt.where(FmeaRow.fmea_type == fmea_type.upper())
    if action_priority:
        stmt = stmt.where(FmeaRow.action_priority == action_priority.upper())
    if unassigned_only:
        stmt = stmt.where(FmeaRow.owner_user_id.is_(None))
    rows = (await session.execute(stmt.order_by(FmeaRow.row_ref))).all()
    return [_fmea_out(r, u) for r, u in rows]


@router.post("/fmea/{row_ref}/owner", response_model=FmeaRowOut,
             summary="Assign a named owner to an FMEA action")
async def assign_owner(row_ref: str, body: FmeaAssignOwner, session: Session,
                       principal: RequireQuality, request: Request) -> FmeaRowOut:
    """A responsibility *function* is not an owner.

    This endpoint is how a High AP item stops blocking G2: someone accountable
    takes it, with a date.
    """
    row = (await session.execute(
        select(FmeaRow).where(FmeaRow.row_ref == row_ref))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown FMEA row {row_ref!r}.")
    owner = (await session.execute(
        select(User).where(User.username == body.owner_username))).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"Unknown user {body.owner_username!r}.")
    row.owner_user_id = owner.id
    if body.target_date:
        row.target_date = body.target_date
    session.add(AuditEvent(actor=principal.username, action="fmea.assign_owner",
                           object_type="FMEA_ROW", object_ref=row_ref,
                           request_id=getattr(request.state, "request_id", None),
                           detail={"owner": owner.username}))
    await session.commit()
    await session.refresh(row)
    return _fmea_out(row, owner)


@router.get("/control-plan", response_model=list[ControlPlanItemOut],
            summary="Control-plan items")
async def control_plan(session: Session, _: CurrentUser,
                       sku: Annotated[str | None, Query()] = None,
                       pending_only: Annotated[bool, Query()] = False
                       ) -> list[ControlPlanItem]:
    stmt = select(ControlPlanItem)
    if sku:
        stmt = stmt.where(ControlPlanItem.sku == sku)
    if pending_only:
        stmt = stmt.where(ControlPlanItem.review_state != ReviewState.CURRENT)
    return list((await session.execute(
        stmt.order_by(ControlPlanItem.control_ref))).scalars().all())


@router.get("/design-parameters", response_model=list[dict],
            summary="Verifiable design characteristics the gates read")
async def parameters(session: Session, _: CurrentUser,
                     sku: Annotated[str | None, Query()] = None) -> list[dict]:
    stmt = select(DesignParameter)
    if sku:
        stmt = stmt.where(DesignParameter.sku == sku)
    rows = (await session.execute(stmt.order_by(DesignParameter.code))).scalars().all()
    return [{"code": p.code, "sku": p.sku, "name": p.name,
             "measured_value": float(p.measured_value) if p.measured_value is not None else None,
             "unit": p.unit,
             "min_value": float(p.min_value) if p.min_value is not None else None,
             "standard_ref": p.standard_ref, "method": p.method,
             "verified": p.verified, "is_placeholder": p.is_placeholder,
             "review_state": p.review_state,
             "meets_minimum": (None if p.measured_value is None or p.min_value is None
                               else p.measured_value >= p.min_value)}
            for p in rows]


@router.post("/design-parameters/{code}/verify", response_model=dict,
             summary="Record a measured value and mark it verified")
async def verify_parameter(code: str, session: Session, principal: RequireEngineer,
                           request: Request,
                           measured_value: Annotated[float, Query()],
                           evidence_artifact: Annotated[str | None, Query()] = None
                           ) -> dict:
    p = (await session.execute(
        select(DesignParameter).where(DesignParameter.code == code))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown parameter {code!r}.")
    if evidence_artifact:
        art = (await session.execute(
            select(Artifact).where(Artifact.name == evidence_artifact))).scalar_one_or_none()
        if art is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail=f"Unknown artifact {evidence_artifact!r}.")
        p.evidence_artifact_id = art.id
    p.measured_value = Decimal(str(measured_value))
    p.verified = True
    p.is_placeholder = False          # a verified measurement is not a placeholder
    p.verified_by = principal.username
    p.verified_at = utcnow()
    p.review_state = ReviewState.CURRENT
    session.add(AuditEvent(actor=principal.username, action="parameter.verify",
                           object_type="DESIGN_PARAMETER", object_ref=code,
                           request_id=getattr(request.state, "request_id", None),
                           detail={"measured_value": measured_value}))
    await session.commit()
    return {"code": p.code, "measured_value": float(p.measured_value),
            "min_value": float(p.min_value) if p.min_value is not None else None,
            "verified": p.verified, "is_placeholder": p.is_placeholder,
            "meets_minimum": (p.min_value is None or p.measured_value >= p.min_value)}


@router.get("/artifacts", response_model=list[ArtifactOut], summary="Artifacts")
async def list_artifacts(session: Session, _: CurrentUser,
                         sku: Annotated[str | None, Query()] = None,
                         artifact_type: Annotated[str | None, Query()] = None,
                         released_only: Annotated[bool, Query()] = False
                         ) -> list[Artifact]:
    stmt = select(Artifact)
    if sku:
        stmt = stmt.where(Artifact.sku == sku)
    if artifact_type:
        stmt = stmt.where(Artifact.artifact_type == artifact_type.upper())
    if released_only:
        stmt = stmt.where(Artifact.status == ArtifactStatus.RELEASED)
    return list((await session.execute(stmt.order_by(Artifact.name))).scalars().all())


@router.post("/artifacts", response_model=ArtifactOut,
             status_code=status.HTTP_201_CREATED, summary="Register an artifact")
async def create_artifact(body: ArtifactCreate, session: Session,
                          principal: RequireEngineer, request: Request) -> Artifact:
    part_id = None
    if body.part_number:
        part = (await session.execute(
            select(Part).where(Part.part_number == body.part_number))).scalar_one_or_none()
        if part is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail=f"Unknown part {body.part_number!r}.")
        part_id = part.id
    data = body.model_dump(exclude={"part_number", "gate_code"})
    art = Artifact(**data, part_id=part_id,
                   gate_code=body.gate_code.value if body.gate_code else None,
                   ingested_at=utcnow(),
                   released_at=utcnow() if body.status == ArtifactStatus.RELEASED else None)
    session.add(art)
    session.add(AuditEvent(actor=principal.username, action="artifact.create",
                           object_type="ARTIFACT", object_ref=body.name,
                           request_id=getattr(request.state, "request_id", None)))
    await session.commit()
    await session.refresh(art)
    return art


@router.get("/compliance/certificates", response_model=list[CertificateOut],
            summary="Certificates on record")
async def certificates(session: Session, _: CurrentUser,
                       sku: Annotated[str | None, Query()] = None) -> list[Certificate]:
    stmt = select(Certificate)
    if sku:
        stmt = stmt.where(Certificate.sku == sku)
    return list((await session.execute(
        stmt.order_by(Certificate.sku, Certificate.scheme))).scalars().all())


@router.post("/compliance/refresh", response_model=ComplianceRefreshResponse,
             summary="Verify BIS CRS, WPC ETA and UN 38.3 against the Compliance Vault")
async def refresh_compliance(session: Session, principal: RequireEngineer,
                             request: Request,
                             sku: Annotated[str, Query()]) -> ComplianceRefreshResponse:
    try:
        out = await compliance_svc.refresh(
            session, sku, actor=principal.username,
            request_id=getattr(request.state, "request_id", None))
    except compliance_svc.ComplianceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return ComplianceRefreshResponse(
        sku=out["sku"], mode=out["mode"], checked=out["checked"], updated=out["updated"],
        certificates=[CertificateOut.model_validate(c) for c in out["certificates"]],
        warnings=out["warnings"])
