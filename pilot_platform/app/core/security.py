"""Authentication (PBKDF2 passwords, HS256 JWT) and role-based access control.

Roles are read from the database on every request rather than trusted from the
token, so disabling a user or removing a role takes effect immediately.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import get_db
from ..models import User

PUBLIC_PATHS = frozenset({"/health", "/auth/login", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"})
WRITER_ROLES = ("founder", "programme_manager", "engineer", "analyst", "procurement", "finance", "qa", "vendor_lead")

ROLES = frozenset({
    "admin", "founder", "programme_manager", "gate_reviewer", "engineer", "analyst",
    "procurement", "finance", "qa", "vendor_lead", "external_reviewer", "viewer",
})


def hash_password(password: str, iterations: int) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iterations))
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def create_access_token(settings: Settings, user: User) -> tuple[str, int]:
    ttl = settings.jwt_ttl_minutes * 60
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user.id), "tenant": user.tenant_id, "iat": now, "exp": now + timedelta(seconds=ttl), "typ": "access"}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256"), ttl


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    tenant_id: str
    roles: frozenset[str]

    def has_any(self, *roles: str) -> bool:
        return "admin" in self.roles or bool(self.roles.intersection(roles))


_bearer = HTTPBearer(auto_error=False)


def current_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Principal:
    if creds is None:
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    settings: Settings = request.app.state.settings
    try:
        payload = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    if payload.get("typ") != "access":
        raise HTTPException(401, "Wrong token type")
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.active or user.tenant_id != payload.get("tenant"):
        raise HTTPException(401, "User disabled or unknown")
    return Principal(user.id, user.username, user.tenant_id, frozenset(user.roles))


def require_roles(*roles: str):
    unknown = set(roles) - ROLES
    if unknown:
        raise ValueError(f"unknown roles: {unknown}")

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_any(*roles):
            raise HTTPException(403, f"Requires one of roles: {', '.join(sorted(roles))}")
        return principal

    return dependency


AUTH_DEPENDENCIES: set = {current_principal}


def register_auth_dependency(fn) -> None:
    """Register a non-JWT authenticator (e.g. HMAC-signed vendor callbacks) as valid route auth."""
    AUTH_DEPENDENCIES.add(fn)


def route_requires_auth(route) -> bool:
    """True if a registered authenticator appears anywhere in the route's dependency tree."""
    stack = list(getattr(route, "dependant", None).dependencies) if getattr(route, "dependant", None) else []
    while stack:
        dep = stack.pop()
        if dep.call in AUTH_DEPENDENCIES:
            return True
        stack.extend(dep.dependencies)
    return False
