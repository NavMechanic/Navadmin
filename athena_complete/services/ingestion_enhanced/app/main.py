"""Athena Ingestion & Parsing Gateway.

Run:  uvicorn app.main:app --port 8007
Docs: http://localhost:8007/docs

The service exists to do one thing well: turn an uploaded document into
parameters with provenance, and to refuse to invent the ones it cannot read.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from .config import get_settings
from .db import create_all, dispose_engine, get_engine, get_sessionmaker
from .extractors import specs as spec_extractor
from .models import DocStatus, Document, ExtractedSpec
from .routers import datasheets as datasheets_router
from .services.vectors import get_index

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("athena.ingest")
_STARTED = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        log.info("database reachable (%s)", settings.database_url.split("@")[-1])
    except Exception as exc:  # noqa: BLE001
        log.error("STARTUP FAILED — database unreachable: %s", exc)
        raise
    await create_all()
    if not settings.refuse_defaults:
        log.error("ATHENA_REFUSE_DEFAULTS=false — this service will substitute "
                  "values it could not extract. That is the behaviour it exists "
                  "to prevent and it is refused in production.")
    log.info("ingestion gateway up — %d extractable parameters, %d safety-relevant",
             len(spec_extractor.PATTERNS), len(spec_extractor.SAFETY_KEYS))
    yield
    await dispose_engine()


app = FastAPI(
    title=settings.app_name, version=settings.version, lifespan=lifespan,
    description=(
        "Ingests datasheets and API payloads, extracts engineering parameters "
        "with evidence, and indexes the text for retrieval.\n\n"
        "**A parameter that cannot be extracted is reported missing. It is never "
        "defaulted.** A defaulted thermal resistance becomes indistinguishable "
        "from a measured one the moment it is stored, and every downstream "
        "calculation inherits it without knowing."),
    docs_url="/docs", openapi_url="/openapi.json")

app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or f"{time.time_ns():x}"[-12:]
    request.state.request_id = rid
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    response.headers["x-process-time-ms"] = (
        f"{(time.perf_counter() - started) * 1000:.1f}")
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "validation_error",
                 "detail": "; ".join(
                     f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}"
                     for e in exc.errors()[:6]),
                 "request_id": getattr(request.state, "request_id", None)})


app.include_router(datasheets_router.router, prefix=settings.api_prefix)


@app.get("/", tags=["meta"])
async def root() -> dict[str, Any]:
    p = settings.api_prefix
    return {"service": settings.app_name, "version": settings.version,
            "node": "Ingestion & Parsing Gateway", "docs": "/docs",
            "endpoints": [f"{p}/datasheets/upload-manual",
                          f"{p}/datasheets/ingest-text",
                          f"{p}/datasheets/held",
                          f"{p}/datasheets/parts/{{part_no}}/parameters",
                          f"{p}/datasheets/search/chunks"]}


@app.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
    factory = get_sessionmaker()
    counts: dict[str, int] = {}
    db_ok, db_error = True, None
    try:
        async with factory() as session:
            counts["documents"] = (await session.execute(
                select(func.count()).select_from(Document))).scalar_one()
            counts["specs"] = (await session.execute(
                select(func.count()).select_from(ExtractedSpec))).scalar_one()
            counts["held"] = (await session.execute(
                select(func.count()).select_from(Document)
                .where(Document.status.in_([DocStatus.HELD,
                                            DocStatus.NEEDS_OCR])))).scalar_one()
    except Exception as exc:  # noqa: BLE001
        db_ok, db_error = False, f"{type(exc).__name__}: {exc}"

    index = get_index()
    caps = {
        "database": {"reachable": db_ok, "error": db_error,
                     "dialect": settings.database_url.split("://")[0]},
        "extraction": {
            "parameters": len(spec_extractor.PATTERNS),
            "safety_relevant": sorted(spec_extractor.SAFETY_KEYS),
            "refuse_defaults": settings.refuse_defaults,
            "hold_incomplete_safety": settings.hold_incomplete_safety},
        "retrieval": index.stats(),
        "workers": {"dispatch_async": settings.dispatch_async,
                    "broker": settings.celery_broker_url.split("@")[-1],
                    "mlflow": settings.mlflow_tracking_uri or None},
    }
    degraded = []
    if not db_ok:
        degraded.append("database unreachable")
    if not settings.refuse_defaults:
        degraded.append("DEFAULTING ENABLED")
    if not index.stats().get("sklearn"):
        degraded.append("retrieval is keyword-only")

    return {"status": "ok" if not degraded else "degraded: " + ", ".join(degraded),
            "version": settings.version, "environment": settings.environment,
            "uptime_seconds": round(time.time() - _STARTED, 1),
            "capabilities": caps, "counts": counts}
