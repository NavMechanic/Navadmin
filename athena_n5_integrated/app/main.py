"""Combined ATHENA N0 + N1 + N2 + N3 + N4 + N5 (+ minimal N6 / N7 / N8 receivers) entry point.

    uvicorn app.main:app          configuration from N0_*, N1, N3_*, N4_* and N5_* environment variables
    create_app(Settings(...), n3_settings=..., n4_settings=..., n5_settings=..., n5_transport=...)   tests

N0 (``app/gateway.py``) is the data-quality gate. N1 (``app/n1``), the minimal N2 PRD service
(``app/n2``), N3 (``app/n3``), N4 (``app/n4``), the minimal N8 receiver (``app/n8``), N5 (``app/n5``) and
the minimal N6 / N7 receivers (``app/n6``, ``app/n7``) are mounted on it through the gateway's extension
hook, in that order (N3 subscribes to N2 PRD releases; N4 and N5 to N2 and N3 releases; N5 to N0
source-rights decisions; N6 / N7 to N5 handoff blocks). They share one database, object store,
malware scanner, audit/approval chain and authenticator. Node roles imply the N0 capabilities they need:

    analyst            → producer
    approver           → gateway_reviewer, release_manager
    service            → producer, node_service
    product_manager    → (none)                 author N2 PRDs
    product_authority  → gateway_reviewer, release_manager   release PRDs to N3
    design_engineer    → producer               N3 baseline, planning, routing, tool jobs, assessment
    vendor_submitter   → (none)                 deliver to work packages routed to its JWT vendor_id
    design_authority   → gateway_reviewer, release_manager   vendor onboarding, design review, N4/N5 release
    test_engineer      → producer               N4 intake, plans, PFMEA / CAPA, live capture, assessment
    lab_importer       → (none)                 lab reports, SCPI logs, AOI, CSV, calibration certificates
    quality_reviewer   → gateway_reviewer       SOP approval, calibration revocation, AOI adjudication, CAPA
    test_authority     → gateway_reviewer, release_manager   approve evidence hash + revision, N8/N6/N7 release
    quality_lead       → gateway_reviewer, release_manager   as test_authority, plus labs and qualifications
    lifecycle_operator → (none)                 N8 intake of released verification packages
    procurement_analyst  → producer             N5 BOM intake, supplier proposals, FX, quotes, lookups, reports
    supplier_quality     → (none)               N5 site verification and supplier qualification
    procurement_approver → gateway_reviewer, release_manager   approve report + quote hashes, N6 / N7 handoff
    commercial_operator  → (none)               N6 intake of sourcing handoffs
    finance_operator     → (none)               N7 intake of sourcing handoffs
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Optional

from fastapi import FastAPI

from .config import Settings
from .gateway import create_app as create_gateway_app
from .models import utcnow
from .n1 import db as _n1_tables  # noqa: F401  registers the N1 tables on the shared metadata
from .n1.config import N1Settings
from .n1.router import install as install_n1
from .n2 import service as _n2_tables  # noqa: F401  registers the N2 tables
from .n2.router import install as install_n2
from .n3 import db as _n3_tables  # noqa: F401  registers the N3 tables
from .n3.config import N3Settings
from .n3.router import install as install_n3
from .n4 import db as _n4_tables  # noqa: F401  registers the N4 tables
from .n4.config import N4Settings
from .n4.router import install as install_n4
from .n5 import db as _n5_tables  # noqa: F401  registers the N5 tables
from .n5.config import N5Settings
from .n5.router import install as install_n5
from .n6 import service as _n6_tables  # noqa: F401  registers the N6 tables
from .n6.router import install as install_n6
from .n7 import service as _n7_tables  # noqa: F401  registers the N7 tables
from .n7.router import install as install_n7
from .n8 import service as _n8_tables  # noqa: F401  registers the N8 tables
from .n8.router import install as install_n8

NODE_ROLES = ["analyst", "approver", "service", "product_manager", "product_authority", "design_engineer",
              "vendor_submitter", "design_authority", "test_engineer", "lab_importer", "quality_reviewer",
              "test_authority", "quality_lead", "lifecycle_operator", "procurement_analyst", "supplier_quality",
              "procurement_approver", "commercial_operator", "finance_operator"]
NODE_ROLE_IMPLICATIONS = {
    "analyst": ["producer"], "approver": ["gateway_reviewer", "release_manager"],
    "service": ["producer", "node_service"], "product_manager": [],
    "product_authority": ["gateway_reviewer", "release_manager"], "design_engineer": ["producer"],
    "vendor_submitter": [], "design_authority": ["gateway_reviewer", "release_manager"],
    "test_engineer": ["producer"], "lab_importer": [], "quality_reviewer": ["gateway_reviewer"],
    "test_authority": ["gateway_reviewer", "release_manager"], "quality_lead": ["gateway_reviewer", "release_manager"],
    "lifecycle_operator": [], "procurement_analyst": ["producer"], "supplier_quality": [],
    "procurement_approver": ["gateway_reviewer", "release_manager"], "commercial_operator": [],
    "finance_operator": []}
# kept for callers of the N0 + N1 build
N1_ROLES = NODE_ROLES
N1_ROLE_IMPLICATIONS = NODE_ROLE_IMPLICATIONS


def with_node_roles(settings: Settings) -> Settings:
    roles = sorted(set(settings.extra_roles) | set(NODE_ROLES))
    implications = {**NODE_ROLE_IMPLICATIONS, **settings.role_implications}
    return settings.model_copy(update={"extra_roles": roles, "role_implications": implications})


with_n1_roles = with_node_roles


def create_app(settings: Optional[Settings] = None, *, n1_settings: Optional[N1Settings] = None,
               n3_settings: Optional[N3Settings] = None, tool_runner=None, n4_settings: Optional[N4Settings] = None,
               n4_provider=None, n5_settings: Optional[N5Settings] = None, n5_transport=None, provider=None,
               clock: Callable[[], datetime] = utcnow, slm_provider=None, explainer_provider=None,
               connector_transport=None, limiter_factory=None) -> FastAPI:
    settings = with_node_roles(settings or Settings.from_env())
    app = create_gateway_app(settings, provider=provider, clock=clock, extensions=[
        install_n1(n1_settings or N1Settings.from_env(), slm_provider=slm_provider,
                   explainer_provider=explainer_provider, transport=connector_transport,
                   limiter_factory=limiter_factory),
        install_n2(),
        install_n3(n3_settings or N3Settings.from_env(), runner=tool_runner),
        install_n4(n4_settings or N4Settings.from_env(), provider=n4_provider),
        install_n8(),
        install_n5(n5_settings or N5Settings.from_env(), transport=n5_transport),
        install_n6(),
        install_n7()])
    app.title = ("ATHENA N0–N5 — Data Quality Gateway, Market, Product and Design Intelligence, Quality & "
                 "Verification, and Procurement, Supplier Intelligence & BOM Optimization (with minimal N6 / N7 / "
                 "N8 receivers)")
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
