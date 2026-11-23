"""Application factory.

    uvicorn app.main:app --reload            # development (SQLite file, demo users)
    uvicorn app.main:create_app --factory    # same, explicit factory form
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .config import Settings, load_settings
from .core.errors import DomainError
from .core.security import hash_password
from .db import Base, make_engine, make_sessionmaker
from .models import ImmutableRowError, Programme, Tenant, User

log = logging.getLogger("pilot_platform")

DEMO_TENANT = "pilot"
DEMO_USERS = {
    "admin": ["admin"], "founder": ["founder"], "pm": ["programme_manager"], "reviewer": ["gate_reviewer"],
    "engineer": ["engineer"], "engineer2": ["engineer"], "analyst": ["analyst"], "procurement": ["procurement"],
    "finance": ["finance"], "qa": ["qa"], "vendor_lead": ["vendor_lead"], "sme": ["external_reviewer"],
    "viewer": ["viewer"],
}


def seed_demo(app: FastAPI) -> None:
    """Create the demo tenant, users (password '<username>-demo') and the pilot programme. Idempotent."""
    settings: Settings = app.state.settings
    with app.state.sessionmaker() as db:
        if db.get(Tenant, DEMO_TENANT) is None:
            db.add(Tenant(id=DEMO_TENANT, name="Pilot Programme", llm_budget_usd=settings.default_llm_budget_usd))
            db.flush()
        for username, roles in DEMO_USERS.items():
            if db.scalars(select(User).where(User.username == username)).first() is None:
                db.add(User(tenant_id=DEMO_TENANT, username=username, roles=roles,
                            password_hash=hash_password(f"{username}-demo", settings.password_iterations)))
        db.flush()
        if db.scalars(select(Programme).where(Programme.tenant_id == DEMO_TENANT)).first() is None:
            from .programme.service import load_default_plan
            load_default_plan(db, DEMO_TENANT, "seed")
        db.commit()
    log.warning("Demo users seeded with known passwords ('<username>-demo'). Never enable PILOT_SEED_DEMO in production.")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = (settings or load_settings()).validate()
    engine = make_engine(settings.database_url)
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)

    app = FastAPI(
        title="Pilot Programme Platform",
        version="0.1.0",
        description="Backend for the pilot CPM network: gated activities A–Z, digital thread, LLM gateway, "
                    "durable orchestration and product nodes N1–N8.",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    # importing these registers workflow definitions/handlers
    from .integration import rfq_pipeline  # noqa: F401
    from .orchestration import selftest  # noqa: F401
    from .api import auth_admin, programme, thread_ai, workflows_nodes

    for module in (auth_admin, programme, thread_ai, workflows_nodes):
        app.include_router(module.router)

    @app.get("/health", tags=["health"])
    def health():
        return {"status": "ok", "env": settings.env, "version": app.version}

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": exc.message, **exc.extra})

    @app.exception_handler(ImmutableRowError)
    async def immutable_error(_: Request, exc: ImmutableRowError):
        return JSONResponse(status_code=409, content={"error": "immutable", "detail": str(exc)})

    if settings.seed_demo:
        seed_demo(app)
    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    """Lazily build `app` so importing this module (e.g. in tests) has no side effects."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
