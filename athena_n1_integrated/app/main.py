"""Combined ATHENA N0 + N1 entry point.

    uvicorn app.main:app          configuration from N0_* and N1 environment variables
    create_app(Settings(...), n1_settings=N1Settings(...), ...)   tests inject settings and providers

N0 (``app/gateway.py``) is the data-quality gate; N1 (``app/n1``) is mounted on it through the
gateway's extension hook. They share one database, object store, audit chain and authenticator.
N1 roles imply the N0 capabilities they need:

    analyst  → producer                       submit market data, build reports, optimize, extract
    approver → gateway_reviewer, release_manager   attest evidence, label, approve N2/N6/N7 handoffs
    service  → producer, node_service         machine ingestion
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Optional

from fastapi import FastAPI

from . import n1  # noqa: F401  (package marker)
from .config import Settings
from .gateway import create_app as create_gateway_app
from .models import utcnow
from .n1 import db as _n1_tables  # noqa: F401  registers the N1 tables on the shared metadata
from .n1.config import N1Settings
from .n1.router import install as install_n1

N1_ROLES = ["analyst", "approver", "service"]
N1_ROLE_IMPLICATIONS = {"analyst": ["producer"], "approver": ["gateway_reviewer", "release_manager"],
                        "service": ["producer", "node_service"]}


def with_n1_roles(settings: Settings) -> Settings:
    roles = sorted(set(settings.extra_roles) | set(N1_ROLES))
    implications = {**N1_ROLE_IMPLICATIONS, **settings.role_implications}
    return settings.model_copy(update={"extra_roles": roles, "role_implications": implications})


def create_app(settings: Optional[Settings] = None, *, n1_settings: Optional[N1Settings] = None, provider=None,
               clock: Callable[[], datetime] = utcnow, slm_provider=None, explainer_provider=None,
               connector_transport=None, limiter_factory=None) -> FastAPI:
    settings = with_n1_roles(settings or Settings.from_env())
    app = create_gateway_app(settings, provider=provider, clock=clock,
                             extensions=[install_n1(n1_settings or N1Settings.from_env(), slm_provider=slm_provider,
                                                    explainer_provider=explainer_provider,
                                                    transport=connector_transport, limiter_factory=limiter_factory)])
    app.title = "ATHENA N0 + N1 — Data Quality Gateway and Market & Opportunity Intelligence Engine"
    return app


_app: Optional[FastAPI] = None


def __getattr__(name: str):   # lazy so importing app.main (tests, schema export) creates no database
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run("app.main:app", host=os.environ.get("N0_HOST", "127.0.0.1"), port=int(os.environ.get("N0_PORT", "8000")))
