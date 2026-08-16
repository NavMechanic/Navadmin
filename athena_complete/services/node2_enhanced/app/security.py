"""JWT authentication and role-based access control.

Design notes:

* Roles are least-privilege and deliberately separated. ``engineer`` can raise
  an ECN but cannot approve it; ``change_board`` approves changes but cannot
  pass a gate; only ``gatekeeper`` transitions gate state. A single "write"
  permission would make the whole stage-gate model decorative.
* ``require_roles`` is a dependency factory, so the permission is declared on
  the route where a reviewer can see it, rather than buried in a handler.
* Token verification is strict: signature, expiry, issuer *and* audience. A
  token minted for another Athena node must not open this one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings
from .enums import Role
from .schemas import Principal

log = logging.getLogger(__name__)

# auto_error=False so a missing header produces our own 401 body rather than
# FastAPI's bare "Not authenticated".
_bearer = HTTPBearer(auto_error=False)


def create_access_token(username: str, roles: list[str], full_name: str | None = None,
                        ttl_minutes: int | None = None) -> tuple[str, datetime]:
    """Mint a signed access token. Returns (token, expiry)."""
    s = get_settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes or s.jwt_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": username,
        "name": full_name,
        "roles": list(roles),
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)
    return token, expires


def decode_token(token: str) -> dict[str, Any]:
    """Verify and decode. Raises HTTPException(401) with a specific reason."""
    s = get_settings()
    try:
        return jwt.decode(
            token, s.jwt_secret, algorithms=[s.jwt_algorithm],
            audience=s.jwt_audience, issuer=s.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail="Token has expired.",
                            headers={"WWW-Authenticate": "Bearer"}) from exc
    except jwt.InvalidAudienceError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail=f"Token audience is not {s.jwt_audience!r}. A token "
                                   "minted for another Athena node is not valid here.",
                            headers={"WWW-Authenticate": "Bearer"}) from exc
    except jwt.InvalidIssuerError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail="Token issuer is not recognised.",
                            headers={"WWW-Authenticate": "Bearer"}) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail=f"Invalid token: {exc}",
                            headers={"WWW-Authenticate": "Bearer"}) from exc


async def get_principal(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal:
    """Resolve the caller.

    With ``ATHENA_AUTH_REQUIRED=false`` an unauthenticated caller is admitted as
    an anonymous viewer — read-only, so turning auth off for a local demo cannot
    silently grant the ability to pass a gate.
    """
    s = get_settings()
    if creds is None or not creds.credentials:
        if not s.auth_required:
            return Principal(username="anonymous", full_name="Anonymous (auth disabled)",
                             roles=[Role.VIEWER])
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token. POST /api/v1/auth/token to obtain one.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_token(creds.credentials)
    raw_roles = claims.get("roles") or []
    roles: list[Role] = []
    for r in raw_roles:
        try:
            roles.append(Role(r))
        except ValueError:
            # An unknown role grants nothing; log it rather than 500 on a token
            # from a newer IdP that knows a role this build does not.
            log.warning("token for %s carries unknown role %r", claims.get("sub"), r)
    principal = Principal(username=str(claims.get("sub")), full_name=claims.get("name"),
                          roles=roles)
    request.state.principal = principal
    return principal


CurrentUser = Annotated[Principal, Depends(get_principal)]


def require_roles(*roles: Role):
    """Dependency factory enforcing that the caller holds at least one role."""

    async def _dep(principal: CurrentUser) -> Principal:
        if not principal.has(*roles):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Requires one of: {', '.join(r.value for r in roles)}. "
                    f"{principal.username} holds: "
                    f"{', '.join(r.value for r in principal.roles) or 'none'}."
                ),
            )
        return principal

    return _dep


# Named dependencies, so routes read as policy rather than plumbing.
RequireViewer = Annotated[Principal, Depends(require_roles(
    Role.VIEWER, Role.ENGINEER, Role.QUALITY, Role.GATEKEEPER,
    Role.CHANGE_BOARD, Role.INTEGRATION, Role.FIELD_QUALITY))]
RequireEngineer = Annotated[Principal, Depends(require_roles(Role.ENGINEER))]
RequireQuality = Annotated[Principal, Depends(require_roles(Role.QUALITY, Role.ENGINEER))]
RequireGatekeeper = Annotated[Principal, Depends(require_roles(Role.GATEKEEPER))]
RequireChangeBoard = Annotated[Principal, Depends(require_roles(Role.CHANGE_BOARD))]
RequireIntegration = Annotated[Principal, Depends(require_roles(Role.INTEGRATION))]

# Opening a CAPA from field evidence is a narrower privilege than owning an FMEA
# row, and it is deliberately separate. Node 6 holds field_quality: it may open a
# CAPA and propose an occurrence re-rating, and it may apply neither a re-rating
# nor a gate decision. Applying a re-rating needs RequireQuality below, which
# field_quality does not satisfy.
RequireFieldQuality = Annotated[Principal, Depends(require_roles(
    Role.FIELD_QUALITY, Role.QUALITY, Role.ENGINEER))]
