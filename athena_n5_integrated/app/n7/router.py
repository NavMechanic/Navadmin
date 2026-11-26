"""Minimal N7 API under ``/v1/n7``. ``finance_operator`` ingests; readers can view."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, FastAPI

from ..errors import N0Error
from ..n6.service import HandoffIntakeRequest
from ..security import Principal, principal_dependency
from .service import N7CostRecord, N7Service

READERS = ("finance_operator", "procurement_approver", "auditor")


def install() -> Callable[[FastAPI], None]:
    def _install(app: FastAPI) -> None:
        svc = N7Service(app.state)
        app.state.n7 = svc
        guarded, auth = app.state.guarded, Depends(principal_dependency)
        r = APIRouter(prefix="/v1/n7", tags=["n7"])

        def run(p, event, subject, roles, fn):
            def go():
                if not p.has(*roles):
                    raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")
                return fn()
            return guarded(p, event, subject, go)

        @r.post("/intake", status_code=201, response_model=N7CostRecord)
        def intake(body: HandoffIntakeRequest, p: Principal = auth) -> N7CostRecord:
            return run(p, "N7_INTAKE", f"artifact:{body.artifact_id}@{body.revision}", ("finance_operator",),
                       lambda: svc.ingest(p, body))

        @r.get("/cost-records/{intake_id}")
        def cost_record(intake_id: str, p: Principal = auth) -> dict:
            return run(p, "N7_READ", f"n7_cost_record:{intake_id}", READERS, lambda: svc.view(p.tenant_id, intake_id))

        app.include_router(r)
    return _install
