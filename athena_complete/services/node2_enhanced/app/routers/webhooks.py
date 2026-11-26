"""CAD/EDA vault webhooks.

The payload is read as raw bytes for signature verification *before* it is
parsed. Verifying a re-serialised body would compare a signature against
something the sender never signed, which is the classic way an HMAC check ends
up not checking anything.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..schemas import VaultIngestResponse, VaultRevisionPayload
from ..security import RequireIntegration
from ..services import vault as vault_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["integrations"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/cad-vault/revision", response_model=VaultIngestResponse,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Ingest a STEP AP242 / Gerber / BOM revision from the vault")
async def cad_vault_revision(
    request: Request,
    session: Session,
    principal: RequireIntegration,
    x_athena_signature: Annotated[str | None, Header()] = None,
    x_athena_timestamp: Annotated[str | None, Header()] = None,
    auto_ecn: bool = True,
) -> VaultIngestResponse:
    """Auto-ingest a vault revision.

    A revision arriving for a part that already has a released revision is an
    engineering change; with ``auto_ecn`` (the default) the ECN is raised and its
    impact analysis runs, so the change cannot quietly reach a gate.
    """
    raw = await request.body()
    try:
        vault_svc.verify_signature(raw, x_athena_signature, x_athena_timestamp)
    except vault_svc.WebhookError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    try:
        payload = VaultRevisionPayload.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                             for e in exc.errors()[:5]),
        ) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail=f"Body is not valid JSON: {exc}") from exc

    try:
        out = await vault_svc.ingest_revision(
            session, payload, actor=principal.username, auto_ecn=auto_ecn,
            request_id=getattr(request.state, "request_id", None))
    except vault_svc.WebhookError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return VaultIngestResponse(**out)


@router.post("/cad-vault/sign", summary="Sign a payload (development helper)",
             response_model=dict)
async def sign_payload(request: Request, _: RequireIntegration) -> dict:
    """Return the signature this service expects for a body.

    Development only — it exists so the demo script and the tests can exercise
    the signed path without reimplementing HMAC in bash.
    """
    raw = await request.body()
    return {"signature": vault_svc.sign(raw), "bytes": len(raw)}
