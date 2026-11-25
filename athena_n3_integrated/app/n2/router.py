"""N2 API under ``/v1/n2``. Roles: ``product_manager`` (author PRDs), ``product_authority`` (release to N3)."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, FastAPI

from ..errors import N0Error
from ..security import Principal, principal_dependency
from .models import PRDCreateRequest, PRDRecord, PRDRelease, PRDReleaseRequest
from .service import N2Service

READERS = ("product_manager", "product_authority", "design_engineer", "design_authority", "auditor", "analyst",
           "approver")


def install() -> Callable[[FastAPI], None]:
    def _install(app: FastAPI) -> None:
        svc = N2Service(app.state)
        app.state.n2 = svc
        guarded, auth = app.state.guarded, Depends(principal_dependency)
        r = APIRouter(prefix="/v1/n2", tags=["n2"])

        def run(p, event, subject, roles, fn):
            def go():
                if not p.has(*roles):
                    raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")
                return fn()
            return guarded(p, event, subject, go)

        @r.post("/prds", response_model=PRDRecord)
        def create(body: PRDCreateRequest, p: Principal = auth) -> PRDRecord:
            return run(p, "N2_PRD_CREATE", f"PRD-{body.prd_id}", ("product_manager",), lambda: svc.create_prd(p, body))

        @r.post("/prds/{prd_id}/revisions/{revision}/release", response_model=PRDRelease)
        def release(prd_id: str, revision: str, body: PRDReleaseRequest, p: Principal = auth) -> PRDRelease:
            return run(p, "N2_PRD_RELEASE", f"PRD-{prd_id}@{revision}", ("product_authority",),
                       lambda: svc.release_prd(p, prd_id, revision, body.reason))

        @r.get("/prds/{prd_id}")
        def view(prd_id: str, p: Principal = auth) -> dict:
            return run(p, "N2_READ", f"PRD-{prd_id}", READERS, lambda: svc.view(p.tenant_id, prd_id))

        app.include_router(r)
    return _install
