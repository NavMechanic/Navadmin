"""Authentication (bearer tokens) and authorization (tenant RBAC + ABAC).

Tokens are configured per deployment (``N0_TOKENS_JSON``) and only their SHA-256 is kept in
memory; comparison is constant-time. Production deployments would front this with OIDC/JWT —
``TokenRegistry.authenticate`` is the single seam to replace.

Roles
  producer          submit uploads, evaluations and extractions for its tenant
  source_admin      register SourceRecord.v1
  rights_approver   approve / reject / revoke source rights (never its own registration)
  gateway_reviewer  resolve REVIEW decisions, record feedback and training labels
  release_manager   approve and execute cross-node handoff releases
  node_service      a node's service identity; ``nodes`` lists which N1–N8 it speaks for
  auditor           read lineage
Attributes: ``tenant_id`` (hard isolation boundary), ``clearance`` (max data classification),
``nodes`` (origin nodes this identity may submit for).
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .models import Confidentiality, NodeId

ROLES = {"producer", "source_admin", "rights_approver", "gateway_reviewer", "release_manager", "node_service",
         "auditor"}


class Principal(BaseModel):
    principal_id: str
    tenant_id: str
    roles: list[str] = Field(default_factory=list)
    nodes: list[NodeId] = Field(default_factory=list)
    clearance: Confidentiality = "CONFIDENTIAL"

    def has(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)


class TokenRegistry:
    def __init__(self, tokens: dict[str, dict]) -> None:
        self._by_hash: dict[str, Principal] = {}
        for token, spec in tokens.items():
            p = Principal(**spec)
            unknown = set(p.roles) - ROLES
            if unknown:
                raise ValueError(f"unknown roles {sorted(unknown)} for {p.principal_id}")
            self._by_hash[hashlib.sha256(token.encode()).hexdigest()] = p

    def authenticate(self, authorization: Optional[str]) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})
        digest = hashlib.sha256(authorization[7:].strip().encode()).hexdigest()
        for h, p in self._by_hash.items():
            if hmac.compare_digest(h, digest):
                return p
        raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})


def principal_dependency(request: Request) -> Principal:
    return request.app.state.tokens.authenticate(request.headers.get("authorization"))


def require_roles(principal: Principal, *roles: str) -> None:
    if not principal.has(*roles):
        raise HTTPException(403, f"requires one of roles {list(roles)}")


def require_tenant(principal: Principal, tenant_id: str) -> None:
    if principal.tenant_id != tenant_id:
        raise HTTPException(403, "cross-tenant access is not permitted")
