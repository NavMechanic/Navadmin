"""Ingestion orchestration: parse, extract, index, decide.

The decision at the end is the point of the service.

After parsing and extraction, a document lands in one of four states:

* ``NEEDS_OCR`` — the file had no text layer. Nothing was extracted and nothing
  could have been. This is reported as its own state because "no parameters
  found in a scanned PDF" and "no parameters found in a readable PDF" call for
  completely different actions.
* ``HELD`` — parsed, but one or more safety-relevant parameters are missing.
  The document is stored and visible; nothing downstream consumes it.
* ``PARSED`` — parsed, non-safety parameters missing, publishable.
* ``PUBLISHED`` — everything requested was found.

At no point does a missing value become a default. That is the whole service.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..extractors import documents as doc_parser
from ..extractors import specs as spec_extractor
from ..models import DocStatus, Document, ExtractedSpec, utcnow
from .vectors import chunk_pages, get_index

log = logging.getLogger(__name__)


class IngestError(ValueError):
    """The upload cannot be processed, with an actionable reason."""


def content_id(blob: bytes, part_no: str | None = None) -> tuple[str, str]:
    """Document id and content hash.

    The id folds in the declared part number so the same bytes uploaded for two
    different parts are two documents. The raw content hash is kept separately
    so "we have seen these exact bytes before" is still answerable.
    """
    sha = hashlib.sha256(blob).hexdigest()
    ident = hashlib.sha256(f"{sha}:{part_no or ''}".encode()).hexdigest()[:32]
    return ident, sha


async def ingest_document(session: AsyncSession, *, blob: bytes, filename: str,
                          part_no: str | None = None,
                          component_type: str | None = None,
                          supplier: str | None = None,
                          sku: str | None = None, revision: str | None = None,
                          uploaded_by: str = "anonymous",
                          content_type: str | None = None,
                          keys: list[str] | None = None) -> dict[str, Any]:
    """Parse, extract, index and decide. Idempotent on content hash."""
    s = get_settings()
    doc_id, sha = content_id(blob, part_no)

    existing = (await session.execute(
        select(Document).where(Document.content_sha256 == sha,
                               Document.part_no == part_no)
    )).scalar_one_or_none()
    if existing is not None:
        specs = (await session.execute(
            select(ExtractedSpec).where(ExtractedSpec.document_id == existing.id)
        )).scalars().all()
        return {
            "document": existing, "specs": list(specs), "duplicate": True,
            "note": (f"This file was already ingested for part "
                     f"{existing.part_no or '(none)'} as {existing.id} on "
                     f"{existing.uploaded_at:%Y-%m-%d}. The stored extraction is "
                     "returned rather than re-run — two extractions of one file "
                     "that disagree is a problem nobody can resolve later. The "
                     "same file uploaded for a different part number is a "
                     "different document."),
        }

    # ---- parse -------------------------------------------------------
    doc = Document(
        id=doc_id, filename=filename, content_sha256=sha, content_type=content_type,
        size_bytes=len(blob), kind="unknown", part_no=part_no,
        component_type=component_type, supplier=supplier, sku=sku,
        revision=revision, uploaded_by=uploaded_by, status=DocStatus.PARSING)
    session.add(doc)

    try:
        parsed = doc_parser.parse(blob, filename)
    except doc_parser.DocumentError as exc:
        doc.status = DocStatus.FAILED
        doc.error = str(exc)
        await session.commit()
        raise IngestError(str(exc)) from exc

    doc.kind = parsed.kind
    doc.page_count = parsed.page_count
    doc.char_count = parsed.char_count
    doc.needs_ocr = parsed.needs_ocr
    doc.warnings = list(parsed.warnings)
    doc.raw_text = parsed.text[:200_000]
    doc.parsed_at = utcnow()

    if parsed.needs_ocr:
        doc.status = DocStatus.NEEDS_OCR
        doc.missing_report = {
            "missing": sorted(keys or [p.key for p in spec_extractor.PATTERNS]),
            "complete": False,
            "reason": ("The document has no text layer. No extraction was "
                       "attempted, because running it would report 'no "
                       "parameters found' — which is true, useless, and easily "
                       "mistaken for a datasheet that genuinely lacks them."),
        }
        await session.commit()
        return {"document": doc, "specs": [], "duplicate": False,
                "note": doc.missing_report["reason"]}

    # ---- extract -----------------------------------------------------
    found = spec_extractor.extract_pages(parsed.pages, keys=keys)
    report = spec_extractor.missing_report(found, requested=keys)
    doc.missing_report = report

    rows: list[ExtractedSpec] = []
    for key, spec in found.items():
        row = ExtractedSpec(
            document_id=doc.id, part_no=part_no, key=key, value=spec.value,
            unit=spec.unit, confidence=spec.confidence.value,
            evidence=spec.evidence, page=spec.page,
            safety_relevant=key in spec_extractor.SAFETY_KEYS)
        session.add(row)
        rows.append(row)

    # ---- index -------------------------------------------------------
    chunks = chunk_pages(doc.id, parsed.pages, part_no=part_no)
    for c in chunks:
        c.metadata = {"filename": filename, "supplier": supplier, "sku": sku}
    doc.chunk_count = get_index().add(chunks)

    # ---- decide ------------------------------------------------------
    if report["safety_relevant_missing"] and s.hold_incomplete_safety:
        doc.status = DocStatus.HELD
        note = (
            f"HELD: {len(report['safety_relevant_missing'])} safety-relevant "
            f"parameter(s) could not be extracted "
            f"({', '.join(report['safety_relevant_missing'])}). The document is "
            "stored and visible and nothing downstream consumes it. No value was "
            "substituted — that is the point of holding rather than publishing.")
    elif report["missing"]:
        doc.status = DocStatus.PARSED
        note = (f"Parsed. {len(report['missing'])} non-safety parameter(s) were "
                "not found and are reported missing rather than defaulted.")
    else:
        doc.status = DocStatus.PUBLISHED
        doc.published_at = utcnow()
        note = "Every requested parameter was extracted with evidence."

    await session.commit()
    log.info("ingested %s (%s) -> %s, %d spec(s), %d chunk(s)",
             filename, doc.id, doc.status, len(rows), doc.chunk_count)
    return {"document": doc, "specs": rows, "duplicate": False, "note": note}


async def publish(session: AsyncSession, document_id: str, *, actor: str,
                  reason: str) -> Document:
    """Release a held document after a human has supplied what was missing."""
    doc = await session.get(Document, document_id)
    if doc is None:
        raise IngestError(f"Unknown document {document_id!r}.")
    if doc.status == DocStatus.NEEDS_OCR:
        raise IngestError(
            f"{document_id} has no text layer. Publishing it would publish a "
            "document nothing could be read from. Run OCR and re-upload.")

    still_missing = [
        k for k in (doc.missing_report or {}).get("safety_relevant_missing", [])
        if not (await session.execute(
            select(ExtractedSpec).where(ExtractedSpec.document_id == document_id,
                                        ExtractedSpec.key == k)
        )).scalar_one_or_none()]
    if still_missing:
        raise IngestError(
            f"{document_id} is still missing safety-relevant parameter(s): "
            f"{', '.join(still_missing)}. Supply them with POST "
            f"/datasheets/{document_id}/specs — which records who supplied the "
            "value and why — rather than publishing around them.")

    doc.status = DocStatus.PUBLISHED
    doc.published_at = utcnow()
    doc.warnings = list(doc.warnings or []) + [
        f"Published by {actor}: {reason}"]
    await session.commit()
    return doc


async def add_manual_spec(session: AsyncSession, document_id: str, *, key: str,
                          value: float, unit: str, actor: str,
                          reason: str) -> ExtractedSpec:
    """Record a human-supplied parameter.

    Confidence is ``MANUAL`` and never one of the extraction confidences: a
    value a person typed in has a different provenance from one read off a page,
    and flattening them into the same field is how "an engineer told us" becomes
    indistinguishable from "the datasheet says".
    """
    doc = await session.get(Document, document_id)
    if doc is None:
        raise IngestError(f"Unknown document {document_id!r}.")
    if key not in spec_extractor.BY_KEY:
        raise IngestError(
            f"Unknown parameter {key!r}. Known: "
            f"{', '.join(sorted(spec_extractor.BY_KEY))}.")
    if not reason or len(reason) < 10:
        raise IngestError(
            "A manual parameter needs a reason. A value with no recorded "
            "justification is the thing this service exists to prevent.")

    pattern = spec_extractor.BY_KEY[key]
    lo, hi = pattern.plausible
    if not (lo <= value <= hi):
        raise IngestError(
            f"{value} is outside the plausible range for {key} ({lo}-{hi} "
            f"{pattern.unit}).")

    existing = (await session.execute(
        select(ExtractedSpec).where(ExtractedSpec.document_id == document_id,
                                    ExtractedSpec.key == key)
    )).scalar_one_or_none()
    if existing is not None:
        existing.correction_reason = (
            f"{existing.correction_reason or ''}\nSuperseded by {actor}: {reason}"
        ).strip()

    row = ExtractedSpec(
        document_id=document_id, part_no=doc.part_no, key=key, value=value,
        unit=unit or pattern.unit, confidence="MANUAL",
        evidence=f"Supplied by {actor}: {reason}", page=None,
        safety_relevant=key in spec_extractor.SAFETY_KEYS,
        corrected_by=actor, correction_reason=reason)
    if existing is not None:
        await session.delete(existing)
    session.add(row)
    await session.commit()
    log.info("manual spec %s=%s on %s by %s", key, value, document_id, actor)
    return row


async def part_parameters(session: AsyncSession, part_no: str) -> dict[str, Any]:
    """Every known parameter for a part, with provenance and disagreements.

    Two datasheets for one part can disagree — a revision B that changed a
    rating, or a distributor sheet copying a manufacturer's with an error. The
    disagreement is surfaced rather than resolved by picking one, because
    picking one silently is how the wrong rating ends up in a thermal model.
    """
    rows = (await session.execute(
        select(ExtractedSpec).where(ExtractedSpec.part_no == part_no)
    )).scalars().all()

    by_key: dict[str, list[ExtractedSpec]] = {}
    for r in rows:
        by_key.setdefault(r.key, []).append(r)

    parameters: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    for key, entries in by_key.items():
        values = {round(e.value, 6) for e in entries}
        best = max(entries, key=lambda e: (
            e.confidence == "MANUAL",
            spec_extractor.CONFIDENCE_RANK.get(
                spec_extractor.Confidence(e.confidence), -1)
            if e.confidence != "MANUAL" else 99))
        parameters[key] = {
            "value": best.value, "unit": best.unit, "confidence": best.confidence,
            "evidence": best.evidence, "page": best.page,
            "document_id": best.document_id,
            "safety_relevant": best.safety_relevant,
            "source_count": len(entries)}
        if len(values) > 1:
            conflicts.append({
                "key": key,
                "values": [{"value": e.value, "unit": e.unit,
                            "document_id": e.document_id,
                            "confidence": e.confidence, "page": e.page,
                            "evidence": e.evidence[:160]} for e in entries],
                "selected": best.value,
                "note": ("Sources disagree on this parameter. The highest-confidence "
                         "value is reported and every source is listed. Resolving "
                         "it is an engineering decision — a revision may have "
                         "changed the rating, or a distributor sheet may have "
                         "copied it wrong."),
            })

    return {"part_no": part_no, "parameter_count": len(parameters),
            "parameters": parameters, "conflicts": conflicts,
            "safety_parameters_present": sorted(
                k for k in parameters if k in spec_extractor.SAFETY_KEYS),
            "safety_parameters_absent": sorted(
                spec_extractor.SAFETY_KEYS - set(parameters))}
