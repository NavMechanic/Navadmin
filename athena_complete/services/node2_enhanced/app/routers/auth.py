"""Development sign-in and token introspection.

In production this router is not mounted: tokens come from the corporate IdP and
``ATHENA_JWT_*`` is pointed at its signing key. It exists so the demo and the
tests can mint a token for a seeded user without standing up an identity
provider.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import User
from ..schemas import Principal, TokenRequest, TokenResponse
from ..security import CurrentUser, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/token", response_model=TokenResponse,
             summary="Mint a token for a seeded user (development only)")
async def token(req: TokenRequest, session: Session) -> TokenResponse:
    user = (await session.execute(
        select(User).where(User.username == req.username))).scalar_one_or_none()
    if user is None or not user.active:
        # Deliberately the same message for "no such user" and "inactive": a
        # sign-in endpoint should not confirm which usernames exist.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail="Unknown or inactive user.")
    access, expires = create_access_token(user.username, list(user.roles), user.full_name)
    return TokenResponse(access_token=access, expires_at=expires,
                         subject=user.username, roles=user.roles)


@router.get("/whoami", response_model=Principal,
            summary="The roles the presented token actually carries")
async def whoami(principal: CurrentUser) -> Principal:
    return principal


@router.get("/roles", summary="What each role may do",
            response_model=dict[str, str])
async def roles() -> dict[str, str]:
    return {
        "viewer": "Read parts, BOMs, gates, ECNs and evaluations.",
        "engineer": "Create parts and revisions, edit BOM structure, raise ECNs, "
                    "upload artifacts.",
        "quality": "Own FMEA rows, complete re-reviews, edit the control plan.",
        "change_board": "Approve or reject engineering changes. Cannot pass a gate.",
        "gatekeeper": "Transition gate state — the only role that can pass or waive a "
                      "gate. Cannot approve the changes it gates.",
        "integration": "Post vault webhooks and connector callbacks.",
        "admin": "All of the above.",
    }
