"""Vendor jobs: dispatch a work package through a connector and ingest the signed result (API A08–A10).

Rules taken from the reference workbook:
  * T35 / DF-026 — data leaves only if every customer RFQ in the inputs' lineage has
    consent_scope VENDOR_ALLOWED (latest revision wins, so a revocation blocks). Otherwise
    the job is BLOCKED_PERMISSION, nothing is sent, and a permission task is logged.
  * T10 — if the vendor's commercial-rights criterion is not currently passed: HELD_LICENSE.
  * T27 — rate limits retry with backoff; the job's idempotency key goes to the vendor and
    charges are de-duplicated by charge_ref, so a retry never double-charges.
  * T32 — a charge with no received artifact is logged as an open exception.
  * T33 — any file whose SHA-256 does not match is QUARANTINED; nothing goes downstream.
  * Results land as digital-thread revisions derived from the job's inputs, in REVIEW state,
    and need a human accept/reject by someone other than the job's creator.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.errors import Conflict, Forbidden, GateBlocked, NotFound, ValidationFailed
from ..core.security import Principal
from ..db import utcnow
from ..models import Artifact, ArtifactRevision, Vendor, VendorConnector, VendorJob
from ..thread import service as thread
from ..vendors.service import FileHandoffAdapter
from . import connectors, service

BLOCKED, HELD, QUEUED, AWAITING, RECEIVED, QUARANTINED, ACCEPTED, REJECTED, FAILED = (
    "BLOCKED_PERMISSION", "HELD_LICENSE", "QUEUED", "AWAITING_VENDOR", "RESULT_RECEIVED", "QUARANTINED",
    "ACCEPTED", "REJECTED", "FAILED")
EXECUTE_ROLES = ("engineer", "procurement", "vendor_lead", "programme_manager")
REVIEW_ROLES = ("engineer", "qa", "programme_manager")
_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class RetryableVendorError(Exception):
    """Raised by adapters for 429/5xx-type failures."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


ADAPTERS: dict[str, object] = {}


def register_adapter(connector_id: str):
    """Register a programmatic adapter: an object with submit(connector, vendor, job) -> dict."""
    def wrap(adapter):
        ADAPTERS[connector_id] = adapter
        return adapter
    return wrap


def _exception(job: VendorJob, code: str, detail: str, open_: bool = True) -> None:
    job.exceptions = job.exceptions + [{"code": code, "detail": detail, "at": utcnow().isoformat(), "open": open_}]


def _resolve(job: VendorJob, code: str) -> None:
    job.exceptions = [{**e, "open": False, "resolved_at": utcnow().isoformat()} if e["code"] == code and e["open"] else e
                      for e in job.exceptions]


def get_job(db: Session, tenant_id: str, job_id: int) -> VendorJob:
    job = db.get(VendorJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise NotFound(f"vendor job {job_id} not found")
    return job


def _rfq_consent(db: Session, tenant_id: str, revision_ids: list[int]) -> tuple[bool, list[str]]:
    """Every RFQ artifact in the inputs' lineage must currently allow vendor sharing."""
    rfq_keys = set()
    for rid in revision_ids:
        rev = thread.get_revision(db, tenant_id, rid)
        if rev.artifact.kind == "RFQ":
            rfq_keys.add(rev.artifact.key)
        rfq_keys |= {a["key"] for a in thread.lineage(db, tenant_id, rid)["ancestors"] if a["kind"] == "RFQ"}
    problems = []
    for key in sorted(rfq_keys):
        latest = thread.latest_revision(db, tenant_id, "RFQ", key)
        scope = latest.content.get("consent_scope", "INTERNAL_ONLY")
        if scope != "VENDOR_ALLOWED":
            problems.append(f"RFQ {key} consent_scope is {scope}")
    return not problems, problems


def _validate_package(wp: dict) -> tuple[list[int], Decimal, str]:
    ids = wp.get("input_revision_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        raise ValidationFailed("work_package.input_revision_ids must be a non-empty list of revision ids")
    if not str(wp.get("deliverable", "")).strip():
        raise ValidationFailed("work_package.deliverable is required")
    budget = wp.get("budget") or {}
    try:
        amount = Decimal(str(budget.get("amount", "")))
    except InvalidOperation:
        raise ValidationFailed("work_package.budget.amount must be a decimal")
    currency = str(budget.get("currency", ""))
    if amount < 0 or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValidationFailed("work_package.budget needs a non-negative amount and an ISO-4217 currency")
    return sorted(set(ids)), amount, currency


def run_job(db: Session, principal: Principal, settings, connector_id: str, work_package: dict,
            idempotency_key: str) -> VendorJob:
    if not principal.has_any(*EXECUTE_ROLES):
        raise Forbidden(f"running vendor jobs needs one of {EXECUTE_ROLES}")
    if not _KEY.match(idempotency_key or ""):
        raise ValidationFailed("idempotency_key must be 1-100 chars of [A-Za-z0-9_.-]")
    existing = db.scalars(select(VendorJob).where(VendorJob.tenant_id == principal.tenant_id,
                                                  VendorJob.idempotency_key == idempotency_key)).first()
    if existing:
        return existing
    conn = connectors.get_connector(db, principal.tenant_id, connector_id)
    if conn.state != "active":
        raise GateBlocked(f"{connector_id} is {conn.state}; only active connectors run jobs (gate G4)",
                          blockers=connectors.readiness(db, principal.tenant_id, conn)["blockers"])
    if conn.reference["thread_kind"] is None:
        raise ValidationFailed(f"{connector_id} does not produce digital-thread artifacts; it is not a job connector")
    vendor = connectors.engagement_vendor(db, principal.tenant_id, connector_id)
    if vendor is None and not conn.reference["internal"]:
        raise GateBlocked(f"{connector_id} has no qualified engagement vendor")
    ids, amount, currency = _validate_package(work_package)
    for rid in ids:
        thread.get_revision(db, principal.tenant_id, rid)  # tenant-scoped existence check
    job = VendorJob(tenant_id=principal.tenant_id, connector_id=connector_id, vendor_id=vendor.id if vendor else None,
                    idempotency_key=idempotency_key, status=QUEUED, work_package=work_package, input_revision_ids=ids,
                    budget_reserved=str(amount), currency=currency, created_by=principal.username)
    db.add(job)
    db.flush()
    audit.record(db, principal.tenant_id, principal.username, "vendor_job.created", "vendor_job", job.id,
                 {"connector_id": connector_id, "budget": f"{amount} {currency}"})
    _clear_or_hold(db, settings, job, conn, vendor, principal.username)
    return job


def _clear_or_hold(db: Session, settings, job: VendorJob, conn: VendorConnector, vendor: Vendor | None, actor: str) -> None:
    """Consent (T35) then licence (T10); dispatch only when both are clear."""
    consent_ok, problems = _rfq_consent(db, job.tenant_id, job.input_revision_ids)
    if not consent_ok:
        if job.status != BLOCKED:
            _exception(job, "consent_missing", "; ".join(problems))
            audit.record(db, job.tenant_id, actor, "permission_task.created", "vendor_job", job.id,
                         {"needs": "customer consent_scope=VENDOR_ALLOWED", "problems": problems})
        job.status, job.blocked_reason = BLOCKED, "; ".join(problems)
        return
    _resolve(job, "consent_missing")
    if vendor is not None and not (service.is_qualified(db, job.tenant_id, vendor.atlas_vendor_id) and
                                   service.criterion_passed(db, job.tenant_id, vendor.atlas_vendor_id, "commercial_rights")):
        if job.status != HELD:
            _exception(job, "license_hold", "vendor due diligence / commercial rights not currently passed")
        job.status, job.blocked_reason = HELD, "vendor due diligence / commercial rights not currently passed"
        return
    _resolve(job, "license_hold")
    job.status, job.blocked_reason = QUEUED, ""
    dispatch(db, settings, job, conn, vendor)


def retry_job(db: Session, principal: Principal, settings, job_id: int) -> VendorJob:
    """Re-check a BLOCKED_PERMISSION / HELD_LICENSE job and dispatch it once consent and rights are cleared."""
    if not principal.has_any(*EXECUTE_ROLES):
        raise Forbidden(f"retrying vendor jobs needs one of {EXECUTE_ROLES}")
    job = get_job(db, principal.tenant_id, job_id)
    if job.status not in (BLOCKED, HELD):
        raise Conflict(f"job {job_id} is {job.status}; only held or blocked jobs can be retried")
    conn = connectors.get_connector(db, principal.tenant_id, job.connector_id)
    if conn.state != "active":
        raise GateBlocked(f"{job.connector_id} is {conn.state}")
    _clear_or_hold(db, settings, job, conn, db.get(Vendor, job.vendor_id) if job.vendor_id else None, principal.username)
    return job


def _inputs_for_vendor(db: Session, job: VendorJob) -> list[dict]:
    revs = [db.get(ArtifactRevision, rid) for rid in job.input_revision_ids]
    return [{"revision_id": r.id, "kind": r.artifact.kind, "key": r.artifact.key, "rev": r.rev,
             "sha256": r.content_hash, "content": r.content} for r in revs]


def dispatch(db: Session, settings, job: VendorJob, conn: VendorConnector, vendor: Vendor | None) -> None:
    job.attempts += 1
    adapter = ADAPTERS.get(conn.connector_id)
    try:
        if adapter is not None:
            result = adapter.submit(conn, vendor, job)
            job.dispatch = {"channel": "adapter", **result}
        else:
            if vendor is None:
                raise ValidationFailed(f"{conn.connector_id} has no adapter and no engagement vendor for a file handoff")
            manifest = {"job_id": job.id, "connector_id": conn.connector_id, "idempotency_key": job.idempotency_key,
                        "deliverable": job.work_package["deliverable"], "instructions": job.work_package.get("instructions", ""),
                        "budget": {"amount": job.budget_reserved, "currency": job.currency},
                        "inputs": _inputs_for_vendor(db, job),
                        "return_to": f"/vendor-callbacks/{job.tenant_id}/{conn.connector_id}"}
            result = FileHandoffAdapter(settings.vendor_outbox_dir).submit(vendor, manifest, f"job-{job.id}")
            programmatic = not (connectors.FILE_ROUTES & set(conn.reference["ingestion_routes"]))
            job.dispatch = {**result, "fallback": programmatic,
                            "note": ("no programmatic adapter registered; manual handoff per registry fallback: "
                                     + conn.reference["gate_fallback"]) if programmatic else conn.reference["gate_fallback"]}
    except RetryableVendorError as exc:
        _exception(job, "rate_limited", str(exc), open_=False)
        if job.attempts >= settings.vendor_job_max_attempts:
            job.status = FAILED
            _exception(job, "dispatch_failed", f"gave up after {job.attempts} attempts")
        else:
            delay = exc.retry_after if exc.retry_after is not None else settings.retry_backoff_seconds * 2 ** (job.attempts - 1)
            job.next_attempt_at = utcnow() + timedelta(seconds=delay)
        return
    job.status, job.next_attempt_at = AWAITING, None
    audit.record(db, job.tenant_id, "engine", "vendor_job.dispatched", "vendor_job", job.id,
                 {"channel": job.dispatch.get("channel"), "attempt": job.attempts})


def dispatch_due(db: Session, settings) -> int:
    """Worker entry point: retry QUEUED jobs whose backoff has elapsed."""
    now, n = utcnow(), 0
    for job in db.scalars(select(VendorJob).where(VendorJob.status == QUEUED)).all():
        if job.next_attempt_at and job.next_attempt_at > now:
            continue
        conn = connectors.get_connector(db, job.tenant_id, job.connector_id)
        if conn.state != "active":
            continue
        dispatch(db, settings, job, conn, db.get(Vendor, job.vendor_id) if job.vendor_id else None)
        n += 1
    db.commit()
    return n


# --- callbacks -------------------------------------------------------------------------

def _decode_files(files: list, max_bytes: int) -> tuple[list[dict], list[str]]:
    decoded, problems, total = [], [], 0
    for i, f in enumerate(files):
        name, declared = str(f.get("name", "")), str(f.get("sha256", "")).lower()
        if not name or not re.fullmatch(r"[0-9a-f]{64}", declared):
            problems.append(f"files[{i}] needs a name and a 64-hex sha256")
            continue
        try:
            data = base64.b64decode(f.get("content_b64", ""), validate=True)
        except (binascii.Error, ValueError):
            problems.append(f"{name}: content_b64 is not valid base64")
            continue
        total += len(data)
        actual = hashlib.sha256(data).hexdigest()
        if not data:
            problems.append(f"{name}: no content supplied; checksum cannot be verified")
        elif actual != declared:
            problems.append(f"{name}: checksum mismatch (declared {declared[:12]}…, actual {actual[:12]}…)")
        decoded.append({"name": Path(name).name, "sha256": actual, "size": len(data), "data": data})
    if total > max_bytes:
        problems.append(f"result files exceed {max_bytes} bytes")
    return decoded, problems


def _store(settings, job: VendorJob, files: list[dict], quarantine: bool) -> list[dict]:
    base = Path(settings.vendor_inbox_dir) / job.tenant_id / ("quarantine" if quarantine else "accepted") / f"job-{job.id}"
    base.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        path = base / f"{f['sha256']}-{f['name']}"
        path.write_bytes(f["data"])
        out.append({"name": f["name"], "sha256": f["sha256"], "size": f["size"], "stored_at": str(path)})
    return out


def receive_callback(db: Session, settings, tenant_id: str, connector_id: str, payload: dict) -> dict:
    event_id, job_id, kind = str(payload.get("event_id", "")), payload.get("job_id"), payload.get("type")
    if not event_id or not isinstance(job_id, int) or kind not in ("job.status", "job.charge", "job.result"):
        raise ValidationFailed("callback needs event_id, integer job_id and type job.status|job.charge|job.result")
    job = get_job(db, tenant_id, job_id)
    if job.connector_id != connector_id:
        raise NotFound(f"vendor job {job_id} not found")
    if event_id in job.events:
        return {"job_id": job.id, "status": job.status, "event": "duplicate_ignored"}
    job.events = job.events + [event_id]
    conn = connectors.get_connector(db, tenant_id, connector_id)

    if kind == "job.charge":
        known = {c["charge_ref"] for c in job.charges}
        for c in payload.get("charges", []):
            ref = str(c.get("charge_ref", ""))
            if not ref or ref in known:
                continue
            try:
                amount = Decimal(str(c["amount"]))
            except (KeyError, InvalidOperation):
                raise ValidationFailed("each charge needs charge_ref and a decimal amount")
            known.add(ref)
            job.charges = job.charges + [{"charge_ref": ref, "amount": str(amount), "currency": c.get("currency", job.currency),
                                          "description": c.get("description", ""), "at": utcnow().isoformat()}]
            if job.status not in (RECEIVED, ACCEPTED):
                _exception(job, "charge_without_artifact", f"charge {ref} ({amount}) received before any accepted artifact")
        spent = sum((Decimal(c["amount"]) for c in job.charges if c["currency"] == job.currency), Decimal("0"))
        if spent > Decimal(job.budget_reserved) and not any(e["code"] == "over_budget" for e in job.exceptions):
            _exception(job, "over_budget", f"charges {spent} exceed reserved {job.budget_reserved} {job.currency}")
    elif kind == "job.status":
        if payload.get("status") == "failed":
            job.status = FAILED
            _exception(job, "vendor_failed", str(payload.get("detail", ""))[:500])
    else:  # job.result
        if job.status != AWAITING:
            raise Conflict(f"job {job.id} is {job.status}; results are accepted only while AWAITING_VENDOR")
        files, problems = _decode_files(payload.get("files", []), settings.vendor_callback_max_bytes)
        if not files:
            problems.append("result has no files")
        if problems:
            job.status = QUARANTINED
            job.result_files = _store(settings, job, [f for f in files if f["data"]], quarantine=True)
            _exception(job, "checksum_mismatch" if any("checksum" in p for p in problems) else "invalid_result",
                       "; ".join(problems))
        else:
            job.result_files = _store(settings, job, files, quarantine=False)
            key = job.work_package.get("artifact_key") or f"{connector_id}-job-{job.id}"
            rev = thread.create_revision(
                db, tenant_id, f"vendor:{conn.reference['provider']}", conn.reference["thread_kind"], key,
                {"approval_state": "REVIEW", "classification": "CONFIDENTIAL", "connector_id": connector_id,
                 "provider": conn.reference["provider"], "job_id": job.id, "tool_version": payload.get("tool_version"),
                 "vendor_result": payload.get("result", {}),
                 "files": [{k: f[k] for k in ("name", "sha256", "size")} for f in job.result_files]},
                derived_from=job.input_revision_ids, note=f"vendor result via {connector_id}")
            job.result_revision_id, job.status = rev.id, RECEIVED
            _resolve(job, "charge_without_artifact")
    audit.record(db, tenant_id, f"vendor:{connector_id}", f"vendor_job.{kind}", "vendor_job", job.id,
                 {"event_id": event_id, "status": job.status})
    return {"job_id": job.id, "status": job.status, "event": "accepted"}


def review(db: Session, principal: Principal, job_id: int, accept: bool, notes: str) -> VendorJob:
    if not principal.has_any(*REVIEW_ROLES):
        raise Forbidden(f"reviewing vendor results needs one of {REVIEW_ROLES}")
    job = get_job(db, principal.tenant_id, job_id)
    if job.status != RECEIVED:
        raise Conflict(f"job {job_id} is {job.status}; only RESULT_RECEIVED jobs can be reviewed")
    if principal.username == job.created_by:
        raise Forbidden("four-eyes rule: the job's creator cannot accept its result")
    rev = db.get(ArtifactRevision, job.result_revision_id)
    art = db.get(Artifact, rev.artifact_id)
    state = "APPROVED" if accept else "REJECTED"
    thread.create_revision(db, principal.tenant_id, principal.username, art.kind, art.key,
                           {**rev.content, "approval_state": state, "reviewed_by": principal.username, "review_notes": notes},
                           note=f"vendor result {state.lower()}")
    job.status, job.reviewed_by = (ACCEPTED if accept else REJECTED), principal.username
    audit.record(db, principal.tenant_id, principal.username, f"vendor_job.{job.status.lower()}", "vendor_job", job.id)
    return job


def job_view(job: VendorJob) -> dict:
    return {"id": job.id, "connector_id": job.connector_id, "vendor_id": job.vendor_id, "status": job.status,
            "idempotency_key": job.idempotency_key, "budget_reserved": job.budget_reserved, "currency": job.currency,
            "attempts": job.attempts, "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else None,
            "dispatch": job.dispatch, "charges": job.charges, "exceptions": job.exceptions,
            "open_exceptions": [e["code"] for e in job.exceptions if e["open"]], "result_files": job.result_files,
            "result_revision_id": job.result_revision_id, "blocked_reason": job.blocked_reason,
            "input_revision_ids": job.input_revision_ids, "created_by": job.created_by, "reviewed_by": job.reviewed_by}
