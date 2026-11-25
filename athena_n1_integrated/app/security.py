"""Authentication (JWT bearer tokens, or static dev tokens) and authorization (tenant RBAC + ABAC).

JWT (production): set ``N0_JWT_SECRET`` (HS256) or ``N0_JWT_PUBLIC_KEY`` (RS256/ES256 PEM from your
identity provider), optionally ``N0_JWT_ISSUER`` / ``N0_JWT_AUDIENCE``. Tokens must carry ``sub``,
``tenant_id`` and ``exp``; ``roles``, ``nodes`` and ``clearance`` are optional claims. The ``none``
algorithm is never accepted and the tenant always comes from the signed token, never from the request.

Static tokens (development and tests): ``N0_TOKENS_JSON``; only their SHA-256 is kept in memory and
comparison is constant-time. Leave it empty in production.

Roles
  producer          submit uploads, evaluations and extractions for its tenant
  source_admin      register SourceRecord.v1
  rights_approver   approve / reject / revoke source rights (never its own registration)
  gateway_reviewer  resolve REVIEW decisions, record feedback and training labels
  release_manager   approve and execute cross-node handoff releases
  node_service      a node's service identity; ``nodes`` lists which N1–N8 it speaks for
  auditor           read lineage and the audit chain
Extensions can register more roles (``Settings.extra_roles``) and implications
(``Settings.role_implications``, e.g. an N1 ``approver`` also acts as ``release_manager``).
Attributes: ``tenant_id`` (hard isolation boundary), ``clearance`` (max data classification),
``nodes`` (origin nodes this identity may submit for).
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Iterable, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .models import Confidentiality, NodeId

ROLES = {"producer", "source_admin", "rights_approver", "gateway_reviewer", "release_manager", "node_service",
         "auditor"}


class Principal(BaseModel):
    principal_id: str
    tenant_id: str
    roles: list[str] = Field(default_factory=list, description="effective roles (assigned + implied)")
    assigned_roles: list[str] = Field(default_factory=list)
    nodes: list[NodeId] = Field(default_factory=list)
    clearance: Confidentiality = "CONFIDENTIAL"
    auth_method: str = "static"

    def has(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)


def _unauthorized(msg: str) -> HTTPException:
    return HTTPException(401, msg, headers={"WWW-Authenticate": "Bearer"})


class Authenticator:
    def __init__(self, tokens: Optional[dict[str, dict]] = None, *, jwt_key: Optional[str] = None,
                 jwt_algorithms: Optional[Iterable[str]] = None, jwt_issuer: Optional[str] = None,
                 jwt_audience: Optional[str] = None, extra_roles: Iterable[str] = (),
                 role_implications: Optional[dict[str, Iterable[str]]] = None, leeway_s: int = 30) -> None:
        self.known_roles = set(ROLES) | set(extra_roles)
        self.implications = {k: set(v) for k, v in (role_implications or {}).items()}
        bad = {r for k, v in self.implications.items() for r in {k, *v}} - self.known_roles
        if bad:
            raise ValueError(f"role implications reference unknown roles {sorted(bad)}")
        self.jwt_key, self.jwt_issuer, self.jwt_audience, self.leeway_s = jwt_key, jwt_issuer, jwt_audience, leeway_s
        algs = list(jwt_algorithms or [])
        if jwt_key and not algs:
            algs = ["RS256", "ES256"] if "BEGIN" in jwt_key else ["HS256"]
        if any(a.lower() == "none" for a in algs):
            raise ValueError("the 'none' JWT algorithm is never allowed")
        self.jwt_algorithms = algs
        self._by_hash: dict[str, Principal] = {}
        for token, spec in (tokens or {}).items():
            self._by_hash[hashlib.sha256(token.encode()).hexdigest()] = self._principal(spec, "static")

    @property
    def jwt_enabled(self) -> bool:
        return bool(self.jwt_key)

    def _principal(self, spec: dict, method: str) -> Principal:
        assigned = list(spec.get("roles") or [])
        unknown = set(assigned) - self.known_roles
        if unknown:
            raise ValueError(f"unknown roles {sorted(unknown)} for {spec.get('principal_id')}")
        effective = set(assigned)
        for r in assigned:
            effective |= self.implications.get(r, set())
        return Principal(**{**spec, "roles": sorted(effective), "assigned_roles": sorted(assigned),
                            "auth_method": method})

    def authenticate(self, authorization: Optional[str]) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _unauthorized("bearer token required")
        token = authorization[7:].strip()
        digest = hashlib.sha256(token.encode()).hexdigest()
        for h, p in self._by_hash.items():
            if hmac.compare_digest(h, digest):
                return p
        if self.jwt_enabled and token.count(".") == 2:
            return self._from_jwt(token)
        raise _unauthorized("invalid token")

    def _from_jwt(self, token: str) -> Principal:
        import jwt  # PyJWT; only needed when JWT is configured
        try:
            claims = jwt.decode(token, self.jwt_key, algorithms=self.jwt_algorithms, issuer=self.jwt_issuer,
                                audience=self.jwt_audience, leeway=self.leeway_s,
                                options={"require": ["exp", "sub", "tenant_id"],
                                         "verify_aud": self.jwt_audience is not None})
        except jwt.PyJWTError as exc:
            raise _unauthorized(f"invalid token: {exc}") from exc
        spec = {"principal_id": str(claims["sub"]), "tenant_id": str(claims["tenant_id"]),
                "roles": claims.get("roles") or [], "nodes": claims.get("nodes") or [],
                "clearance": claims.get("clearance", "CONFIDENTIAL")}
        try:
            return self._principal(spec, "jwt")
        except (ValueError, ValidationError) as exc:
            raise _unauthorized(f"invalid token claims: {exc}") from exc


class TokenRegistry(Authenticator):
    """Backwards-compatible name: static tokens only."""

    def __init__(self, tokens: dict[str, dict]) -> None:
        super().__init__(tokens)


def build_authenticator(settings) -> Authenticator:
    return Authenticator(settings.tokens, jwt_key=settings.jwt_secret or settings.jwt_public_key,
                         jwt_algorithms=settings.jwt_algorithms, jwt_issuer=settings.jwt_issuer,
                         jwt_audience=settings.jwt_audience, extra_roles=settings.extra_roles,
                         role_implications=settings.role_implications)


def principal_dependency(request: Request) -> Principal:
    return request.app.state.tokens.authenticate(request.headers.get("authorization"))


def require_roles(principal: Principal, *roles: str) -> None:
    if not principal.has(*roles):
        raise HTTPException(403, f"requires one of roles {list(roles)}")


def require_tenant(principal: Principal, tenant_id: str) -> None:
    if principal.tenant_id != tenant_id:
        raise HTTPException(403, "cross-tenant access is not permitted")
