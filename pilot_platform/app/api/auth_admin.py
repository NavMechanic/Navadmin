"""Auth, user administration, tenant budget and audit endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.security import (ROLES, Principal, create_access_token, current_principal, hash_password, require_roles,
                             verify_password)
from ..db import get_db
from ..models import AuditEvent, Tenant, User

router = APIRouter()


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/auth/login", tags=["auth"])
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = db.scalars(select(User).where(User.username == body.username)).first()
    if user is None or not user.active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    token, ttl = create_access_token(request.app.state.settings, user)
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl,
            "user": {"username": user.username, "tenant_id": user.tenant_id, "roles": user.roles}}


@router.get("/auth/me", tags=["auth"])
def me(p: Principal = Depends(current_principal)):
    return {"username": p.username, "tenant_id": p.tenant_id, "roles": sorted(p.roles)}


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=12)
    roles: list[str]


@router.post("/admin/users", tags=["admin"], status_code=201)
def create_user(body: UserIn, request: Request, p: Principal = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    unknown = set(body.roles) - ROLES
    if unknown or not body.roles:
        raise HTTPException(422, f"roles must be a non-empty subset of {sorted(ROLES)}")
    if db.scalars(select(User).where(User.username == body.username)).first():
        raise HTTPException(409, "username taken")
    user = User(tenant_id=p.tenant_id, username=body.username, roles=sorted(set(body.roles)),
                password_hash=hash_password(body.password, request.app.state.settings.password_iterations))
    db.add(user)
    db.flush()
    audit.record(db, p.tenant_id, p.username, "user.created", "user", user.id, {"roles": user.roles})
    db.commit()
    return {"id": user.id, "username": user.username, "roles": user.roles}


@router.get("/admin/users", tags=["admin"])
def list_users(p: Principal = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    users = db.scalars(select(User).where(User.tenant_id == p.tenant_id).order_by(User.id)).all()
    return [{"id": u.id, "username": u.username, "roles": u.roles, "active": u.active} for u in users]


class BudgetIn(BaseModel):
    llm_budget_usd: float = Field(ge=0, le=1_000_000)


@router.put("/admin/tenant/budget", tags=["admin"])
def set_budget(body: BudgetIn, p: Principal = Depends(require_roles("admin", "finance")), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, p.tenant_id)
    old = tenant.llm_budget_usd
    tenant.llm_budget_usd = body.llm_budget_usd
    audit.record(db, p.tenant_id, p.username, "tenant.budget_changed", "tenant", p.tenant_id,
                 {"old": old, "new": body.llm_budget_usd})
    db.commit()
    return {"tenant_id": p.tenant_id, "llm_budget_usd": tenant.llm_budget_usd}


@router.get("/audit", tags=["audit"])
def list_audit(limit: int = 100, p: Principal = Depends(require_roles("programme_manager")), db: Session = Depends(get_db)):
    events = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == p.tenant_id)
                        .order_by(AuditEvent.seq.desc()).limit(min(limit, 1000))).all()
    return [{"seq": e.seq, "actor": e.actor, "action": e.action, "entity": f"{e.entity_type}:{e.entity_id}",
             "payload": e.payload, "at": e.created_at.isoformat(), "hash": e.hash} for e in events]


@router.get("/audit/verify", tags=["audit"])
def verify_audit(p: Principal = Depends(require_roles("programme_manager")), db: Session = Depends(get_db)):
    return audit.verify_chain(db, p.tenant_id)
