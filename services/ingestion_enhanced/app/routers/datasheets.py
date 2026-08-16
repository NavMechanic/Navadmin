"""Datasheet ingestion endpoints."""
from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
                     Query, Request, UploadFile, status)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..extractors import specs as spec_extractor
from ..models import DocStatus, Document, ExtractedSpec
from ..security import ServiceCaller
from ..services import ingest as svc
from ..services.vectors import get_index

log = logging.getLogger(__name__)
router = APIRouter(prefix="/datasheets", tags=["datasheets"])
Session = Annotated[AsyncSession, Depends(get_session)]


class DatasheetMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_type: str | None = Field(default=None, max_length=64)
    ref_part_no: str | None = Field(default=None, max_length=64)
    supplier: str | None = Field(default=None, max_length=120)
    sku: str | None = Field(default=None, max_length=32)
    revision: str | None = Field(default=None, max_length=32)
    # Which parameters this upload is expected to carry. Naming them turns a
    # silent absence into a reported one.
    expect: list[str] | None = None


class TextIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=20, max_length=2_000_000)
    filename: str = Field(default="payload.txt", max_length=255)
    metadata: DatasheetMetadata = Field(default_factory=DatasheetMetadata)


class ManualSpecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: float
    unit: str | None = None
    reason: str = Field(min_length=10)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=10)


def _doc_out(doc: Document, specs: list[ExtractedSpec]) -> dict[str, Any]:
    return {
        "document_id": doc.id, "filename": doc.filename, "kind": doc.kind,
        "status": doc.status, "part_no": doc.part_no, "supplier": doc.supplier,
        "sku": doc.sku, "revision": doc.revision,
        "page_count": doc.page_count, "char_count": doc.char_count,
        "chunk_count": doc.chunk_count, "needs_ocr": doc.needs_ocr,
        "uploaded_by": doc.uploaded_by,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
        "extracted": [
            {"key": s.key, "value": s.value, "unit": s.unit,
             "confidence": s.confidence, "page": s.page,
             "safety_relevant": s.safety_relevant, "evidence": s.evidence}
            for s in sorted(specs, key=lambda x: x.key)],
        "missing_report": doc.missing_report,
        "warnings": doc.warnings, "error": doc.error,
    }


@router.post("/upload-manual", status_code=status.HTTP_201_CREATED,
             summary="Upload a PDF/CSV/XLSX datasheet")
async def upload(session: Session, caller: ServiceCaller, request: Request,
                 file: UploadFile = File(...),
                 metadata: str = Form("{}")) -> dict[str, Any]:
    """Parse, extract, index and decide — synchronously.

    Synchronous on purpose. The reference design queued this to Celery and
    returned ``{"status": "QUEUED"}``, which means the person who uploaded the
    datasheet never learns that its thermal resistance could not be read. The
    extraction takes well under a second for a datasheet; the *retraining* it
    may trigger is what belongs on a queue, and that is what is dispatched
    asynchronously below.
    """
    s = get_settings()
    try:
        meta = DatasheetMetadata(**json.loads(metadata or "{}"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail=f"metadata is not valid JSON: {exc}")
    except Exception as exc:  # noqa: BLE001 - pydantic validation
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Invalid metadata: {exc}")

    blob = await file.read()
    if len(blob) > s.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {s.max_upload_mb} MB cap.")

    try:
        result = await svc.ingest_document(
            session, blob=blob, filename=file.filename or "upload",
            part_no=meta.ref_part_no, component_type=meta.component_type,
            supplier=meta.supplier, sku=meta.sku, revision=meta.revision,
            uploaded_by=caller, content_type=file.content_type, keys=meta.expect)
    except svc.IngestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    doc: Document = result["document"]
    body = _doc_out(doc, result["specs"])
    body["duplicate"] = result["duplicate"]
    body["note"] = result["note"]

    # The retraining IS queued — that is the part that takes minutes.
    if doc.status in (DocStatus.PARSED, DocStatus.PUBLISHED) and result["specs"]:
        body["dispatched"] = _dispatch(doc, result["specs"])
    return body


@router.post("/ingest-text", status_code=status.HTTP_201_CREATED,
             summary="Ingest a text payload (API path, no file upload)")
async def ingest_text(body: TextIngestRequest, session: Session,
                      caller: ServiceCaller) -> dict[str, Any]:
    try:
        result = await svc.ingest_document(
            session, blob=body.text.encode(), filename=body.filename,
            part_no=body.metadata.ref_part_no,
            component_type=body.metadata.component_type,
            supplier=body.metadata.supplier, sku=body.metadata.sku,
            revision=body.metadata.revision, uploaded_by=caller,
            content_type="text/plain", keys=body.metadata.expect)
    except svc.IngestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    out = _doc_out(result["document"], result["specs"])
    out["duplicate"] = result["duplicate"]
    out["note"] = result["note"]
    return out


def _dispatch(doc: Document, specs: list[ExtractedSpec]) -> dict[str, Any]:
    """Hand the extracted parameters to the worker fleet.

    Never raises. A broker that is down must not fail an ingestion that already
    succeeded — the document and its parameters are stored either way, and the
    dispatch result says plainly whether the queue took it.
    """
    s = get_settings()
    payload = {
        "document_id": doc.id, "part_no": doc.part_no, "supplier": doc.supplier,
        "sku": doc.sku,
        "specs": {sp.key: {"value": sp.value, "unit": sp.unit,
                           "confidence": sp.confidence} for sp in specs},
    }
    if not s.dispatch_async:
        return {"queued": False, "reason": "ATHENA_DISPATCH_ASYNC=false"}
    try:
        from workers.tasks import task_register_parameters
        async_result = task_register_parameters.delay(payload)
        doc.task_id = async_result.id
        return {"queued": True, "task_id": async_result.id,
                "task": "task_register_parameters"}
    except Exception as exc:  # noqa: BLE001
        log.warning("could not queue the retraining task: %s", exc)
        return {"queued": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "note": ("The document and its parameters are stored. Only the "
                         "downstream model registration was not queued, and it "
                         "can be replayed from the stored record.")}


@router.get("", summary="List ingested documents")
async def listing(session: Session, doc_status: str | None = None,
                  part_no: str | None = None,
                  limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    stmt = select(Document).order_by(Document.uploaded_at.desc()).limit(limit)
    if doc_status:
        stmt = stmt.where(Document.status == doc_status)
    if part_no:
        stmt = stmt.where(Document.part_no == part_no)
    docs = (await session.execute(stmt)).scalars().all()
    return {"count": len(docs), "documents": [
        {"document_id": d.id, "filename": d.filename, "status": d.status,
         "part_no": d.part_no, "supplier": d.supplier, "kind": d.kind,
         "page_count": d.page_count, "needs_ocr": d.needs_ocr,
         "uploaded_by": d.uploaded_by,
         "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
         "missing_safety": (d.missing_report or {}).get(
             "safety_relevant_missing", [])} for d in docs]}


@router.get("/held", summary="Documents held for missing safety parameters")
async def held(session: Session) -> dict[str, Any]:
    docs = (await session.execute(
        select(Document).where(Document.status.in_(
            [DocStatus.HELD, DocStatus.NEEDS_OCR]))
        .order_by(Document.uploaded_at.desc()))).scalars().all()
    return {
        "count": len(docs),
        "documents": [{"document_id": d.id, "filename": d.filename,
                       "status": d.status, "part_no": d.part_no,
                       "missing": (d.missing_report or {}).get(
                           "safety_relevant_missing", []),
                       "consequences": (d.missing_report or {}).get(
                           "consequences", [])} for d in docs],
        "note": ("These documents are stored and nothing downstream consumes "
                 "them. Supply the missing values with POST "
                 "/datasheets/{id}/specs, then publish."),
    }


@router.get("/{document_id}", summary="One document with its extraction")
async def detail(document_id: str, session: Session) -> dict[str, Any]:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"Unknown document {document_id!r}.")
    specs = (await session.execute(
        select(ExtractedSpec).where(ExtractedSpec.document_id == document_id)
    )).scalars().all()
    return _doc_out(doc, list(specs))


@router.post("/{document_id}/specs", status_code=status.HTTP_201_CREATED,
             summary="Supply a parameter the extractor could not read")
async def add_spec(document_id: str, body: ManualSpecRequest, session: Session,
                   caller: ServiceCaller) -> dict[str, Any]:
    try:
        row = await svc.add_manual_spec(
            session, document_id, key=body.key, value=body.value,
            unit=body.unit or "", actor=caller, reason=body.reason)
    except svc.IngestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return {"key": row.key, "value": row.value, "unit": row.unit,
            "confidence": row.confidence, "recorded_by": row.corrected_by,
            "note": ("Confidence is MANUAL, never an extraction confidence. A "
                     "value a person typed has different provenance from one "
                     "read off a page.")}


@router.post("/{document_id}/publish", summary="Publish a held document")
async def publish(document_id: str, body: PublishRequest, session: Session,
                  caller: ServiceCaller) -> dict[str, Any]:
    try:
        doc = await svc.publish(session, document_id, actor=caller,
                                reason=body.reason)
    except svc.IngestError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    return {"document_id": doc.id, "status": doc.status,
            "published_at": doc.published_at.isoformat() if doc.published_at else None}


@router.get("/parts/{part_no}/parameters",
            summary="Every known parameter for a part, with provenance")
async def part_parameters(part_no: str, session: Session) -> dict[str, Any]:
    return await svc.part_parameters(session, part_no)


@router.get("/search/chunks", summary="Retrieve document passages")
async def search(q: str = Query(min_length=2), top_k: int = Query(5, ge=1, le=50),
                 part_no: str | None = None) -> dict[str, Any]:
    index = get_index()
    return {"query": q, "backend": index.backend,
            "results": index.search(q, top_k=top_k, part_no=part_no),
            "stats": index.stats()}


@router.get("/schema/parameters", summary="What this extractor knows how to read")
async def parameter_schema() -> dict[str, Any]:
    return {
        "parameters": [
            {"key": p.key, "unit": p.unit, "safety_relevant": p.safety_relevant,
             "plausible_range": list(p.plausible), "description": p.description,
             "pattern_count": len(p.patterns)}
            for p in spec_extractor.PATTERNS],
        "confidence_levels": [c.value for c in spec_extractor.Confidence],
        "policy": ("A parameter that cannot be extracted is reported missing and "
                   "never defaulted. Values outside the plausible range are "
                   "refused rather than clamped — a parse that yields a 900 K/W "
                   "thermal resistance has matched the wrong number on the page."),
    }
