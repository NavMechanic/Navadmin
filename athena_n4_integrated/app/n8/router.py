"""Minimal N8 API under ``/v1/n8``. Role ``lifecycle_operator`` ingests; ``auditor`` can read."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, FastAPI

from ..errors import N0Error
from ..security import Principal, principal_dependency
from .service import N8Intake, N8IntakeRequest, N8Service

READERS = ("lifecycle_operator", "auditor", "test_authority", "quality_lead")


def install() -> Callable[[FastAPI], None]:
    def _install(app: FastAPI) -> None:
        svc = N8Service(app.state)
        app.state.n8 = svc
        guarded, auth = app.state.guarded, Depends(principal_dependency)
        r = APIRouter(prefix="/v1/n8", tags=["n8"])

        def run(p, event, subject, roles, fn):
            def go():
                if not p.has(*roles):
                    raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")
                return fn()
            return guarded(p, event, subject, go)

        @r.post("/intake", status_code=201, response_model=N8Intake)
        def intake(body: N8IntakeRequest, p: Principal = auth) -> N8Intake:
            return run(p, "N8_INTAKE", f"artifact:{body.artifact_id}@{body.revision}", ("lifecycle_operator",),
                       lambda: svc.ingest(p, body))

        @r.get("/units/{serial}")
        def unit(serial: str, p: Principal = auth) -> dict:
            return run(p, "N8_READ", f"unit:{serial}", READERS, lambda: svc.unit(p.tenant_id, serial))

        @r.get("/monitors")
        def monitors(p: Principal = auth) -> list[dict]:
            return run(p, "N8_READ", "n8_monitors", READERS, lambda: svc.monitors(p.tenant_id))

        app.include_router(r)
    return _install
