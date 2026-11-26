"""Minimal N6 API under ``/v1/n6``. ``commercial_operator`` ingests; readers can view."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, FastAPI

from ..errors import N0Error
from ..security import Principal, principal_dependency
from .service import HandoffIntakeRequest, N6BidInput, N6Service

READERS = ("commercial_operator", "procurement_approver", "auditor")


def install() -> Callable[[FastAPI], None]:
    def _install(app: FastAPI) -> None:
        svc = N6Service(app.state)
        app.state.n6 = svc
        guarded, auth = app.state.guarded, Depends(principal_dependency)
        r = APIRouter(prefix="/v1/n6", tags=["n6"])

        def run(p, event, subject, roles, fn):
            def go():
                if not p.has(*roles):
                    raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")
                return fn()
            return guarded(p, event, subject, go)

        @r.post("/intake", status_code=201, response_model=N6BidInput)
        def intake(body: HandoffIntakeRequest, p: Principal = auth) -> N6BidInput:
            return run(p, "N6_INTAKE", f"artifact:{body.artifact_id}@{body.revision}", ("commercial_operator",),
                       lambda: svc.ingest(p, body))

        @r.get("/bid-inputs/{intake_id}")
        def bid_input(intake_id: str, p: Principal = auth) -> dict:
            return run(p, "N6_READ", f"n6_bid_input:{intake_id}", READERS, lambda: svc.view(p.tenant_id, intake_id))

        app.include_router(r)
    return _install
