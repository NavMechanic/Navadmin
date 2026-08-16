"""Athena Node 2 — Micro-PLM & Stage-Gate Governance.

Run:  uvicorn app.main:app --reload --port 8002
Docs: http://localhost:8002/docs
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from .config import get_settings
from .db import create_all, dispose_engine, get_engine, get_sessionmaker
from .models import (Artifact, BomLine, Capa, Certificate, ECN, FmeaRow,
                     GateInstance, Part, PartRevision, User)
from .routers import auth as auth_router
from .routers import bom as bom_router
from .routers import capa as capa_router
from .routers import ecn as ecn_router
from .routers import gates as gates_router
from .routers import records as records_router
from .routers import webhooks as webhooks_router
from .schemas import ErrorBody, HealthResponse
from .gates.registry import all_rules

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("athena.node2")
_STARTED = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on an unreachable database. A PLM service that starts without
    # its system of record will answer "no gate found" to every question, which
    # reads exactly like a clean programme.
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        log.info("database reachable (%s)", settings.database_url.split("@")[-1])
    except Exception as exc:  # noqa: BLE001
        log.error("STARTUP FAILED — database unreachable: %s", exc)
        raise

    if settings.is_sqlite:
        # SQLite is the no-server path; create the schema in place. On
        # PostgreSQL the schema belongs to Alembic.
        await create_all()
        log.info("sqlite schema ensured (use alembic upgrade head on PostgreSQL)")

    log.info("%d gate rules registered across %d gates", len(all_rules()),
             len({r.gate for r in all_rules()}))
    yield
    await dispose_engine()
    log.info("shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "Node 2 of Project Athena. System of record for multi-level BOMs and their "
        "revision history, the ECN/ECO workflow with automatic where-used impact "
        "analysis, and a system-enforced 8-stage gate engine that refuses a workflow "
        "transition when required evidence is missing."
    ),
    lifespan=lifespan, docs_url="/docs", redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error on %s %s [rid=%s]",
                      request.method, request.url.path, rid)
        raise
    took = (time.perf_counter() - start) * 1000
    response.headers["x-request-id"] = rid
    response.headers["x-process-time-ms"] = f"{took:.1f}"
    if took > 2000:
        log.warning("slow request %s %s took %.0f ms [rid=%s]",
                    request.method, request.url.path, took, rid)
    return response


# ---------------------------------------------------------------- errors
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorBody(
            error="validation_error",
            detail="; ".join(f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}"
                             for e in exc.errors()[:6]),
            hint="Check the request body against /docs. Every field is typed and "
                 "unknown fields are rejected.",
            request_id=getattr(request.state, "request_id", None),
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorBody(
            error="internal_error", detail=f"{type(exc).__name__}: {exc}",
            hint="Check the service log for the traceback against this request id.",
            request_id=getattr(request.state, "request_id", None),
        ).model_dump(),
    )


# ---------------------------------------------------------------- routes
for r in (auth_router.router, bom_router.router, gates_router.router,
          ecn_router.router, capa_router.router, records_router.router,
          webhooks_router.router):
    app.include_router(r, prefix=settings.api_prefix)


@app.get("/", tags=["meta"], summary="Service banner")
async def root() -> dict:
    p = settings.api_prefix
    return {
        "service": settings.app_name,
        "version": settings.version,
        "node": "N2 — Micro-PLM & Stage-Gate Governance",
        "docs": "/docs",
        "endpoints": [
            f"{p}/auth/token",
            f"{p}/plm/gates/{{gate_id}}/evaluate",
            f"{p}/plm/gates/{{gate_id}}/waive",
            f"{p}/plm/gates/{{gate_id}}/rules",
            f"{p}/bom/{{part_number}}/explode",
            f"{p}/bom/{{part_number}}/where-used",
            f"{p}/ecn",
            f"{p}/plm/capa",
            f"{p}/plm/capa/{{ref}}/rerating",
            f"{p}/ecn/{{ecn_ref}}/impacts",
            f"{p}/fmea",
            f"{p}/compliance/refresh",
            f"{p}/webhooks/cad-vault/revision",
        ],
    }


@app.get("/health", response_model=HealthResponse, tags=["meta"],
         summary="Liveness, database and capability status")
async def health() -> HealthResponse:
    factory = get_sessionmaker()
    counts: dict[str, int] = {}
    db_ok, db_error = True, None
    try:
        async with factory() as session:
            for label, model in (("parts", Part), ("revisions", PartRevision),
                                 ("bom_lines", BomLine), ("artifacts", Artifact),
                                 ("certificates", Certificate), ("fmea_rows", FmeaRow),
                                 ("gates", GateInstance), ("ecns", ECN), ("capas", Capa),
                                 ("users", User)):
                counts[label] = (await session.execute(
                    select(func.count()).select_from(model))).scalar_one()
    except Exception as exc:  # noqa: BLE001
        db_ok, db_error = False, f"{type(exc).__name__}: {exc}"

    seeded = counts.get("parts", 0) > 0
    return HealthResponse(
        status="ok" if (db_ok and seeded) else "degraded",
        version=settings.version, environment=settings.environment,
        uptime_seconds=round(time.time() - _STARTED, 1),
        database={
            "reachable": db_ok,
            "dialect": settings.database_url.split("://")[0],
            "target": settings.database_url.split("@")[-1],
            "seeded": seeded,
            "error": db_error,
            "hint": None if seeded else "Run: python3 scripts/seed.py",
        },
        capabilities={
            "gate_rules": len(all_rules()),
            "gates": sorted({r.gate.value for r in all_rules()}),
            "auth_required": settings.auth_required,
            "compliance_vault": "replay" if settings.compliance_offline else "live",
            "cad_vault": "webhook-only" if settings.cad_vault_offline else "live",
            "creepage_min_mm_adapter": settings.creepage_min_mm_adapter,
            "capacity_of_policy": {
                "fmea_high_ap_requires_named_owner":
                    settings.fmea_high_ap_requires_named_owner,
                "enforce_child_gate_precedence": settings.enforce_child_gate_precedence,
                "certificate_min_validity_days": settings.certificate_min_validity_days,
            },
        },
        counts=counts,
    )
