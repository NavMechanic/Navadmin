"""Specialist vendor registry and adapters (activity L) and vendor POC QA (activity T).

* Qualification requires a verified licence, accepted data terms, and a usable channel:
  an API endpoint, or a declared file-handoff format (the manual fallback in the plan).
* `FileHandoffAdapter` writes a job manifest to an outbox directory keyed by an
  idempotency key, so re-sending the same job overwrites rather than duplicates.
* API adapters are vendor-specific; `ApiAdapter` is the extension point and refuses to
  pretend a call happened until a concrete adapter is registered.
* Scorecards (gate T) must record a decision plus evidence; "accept" additionally needs
  every criterion to be true.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.errors import Conflict, NotFound, ValidationFailed
from ..core.security import Principal
from ..models import Vendor

MODES = {"api", "file_handoff"}
CRITERIA = ("output_format_ok", "acceptance_met", "ip_rights_confirmed", "charges_within_quote")


def create_vendor(db: Session, principal: Principal, data: dict) -> Vendor:
    if data.get("mode") not in MODES:
        raise ValidationFailed(f"mode must be one of {sorted(MODES)}")
    v = Vendor(tenant_id=principal.tenant_id, name=data["name"], capability=data.get("capability", "general"),
               mode=data["mode"], endpoint=data.get("endpoint", ""), handoff_format=data.get("handoff_format", ""),
               license_verified=bool(data.get("license_verified")), data_terms_accepted=bool(data.get("data_terms_accepted")))
    db.add(v)
    db.flush()
    audit.record(db, principal.tenant_id, principal.username, "vendor.created", "vendor", v.id, {"name": v.name})
    return v


def get_vendor(db: Session, tenant_id: str, vendor_id: int) -> Vendor:
    v = db.get(Vendor, vendor_id)
    if v is None or v.tenant_id != tenant_id:
        raise NotFound(f"vendor {vendor_id} not found")
    return v


def qualify(db: Session, principal: Principal, vendor_id: int) -> Vendor:
    v = get_vendor(db, principal.tenant_id, vendor_id)
    missing = []
    if not v.license_verified:
        missing.append("license_verified")
    if not v.data_terms_accepted:
        missing.append("data_terms_accepted")
    if v.mode == "api" and not v.endpoint:
        missing.append("endpoint")
    if v.mode == "file_handoff" and not v.handoff_format:
        missing.append("handoff_format")
    if missing:
        raise ValidationFailed("vendor cannot be qualified", missing=missing)
    v.status = "qualified"
    audit.record(db, principal.tenant_id, principal.username, "vendor.qualified", "vendor", v.id)
    return v


def record_scorecard(db: Session, principal: Principal, vendor_id: int, decision: str, criteria: dict,
                     evidence: list[str], notes: str = "") -> Vendor:
    v = get_vendor(db, principal.tenant_id, vendor_id)
    if decision not in ("accept", "reject"):
        raise ValidationFailed("decision must be accept or reject")
    evidence = [e for e in evidence if str(e).strip()]
    if not evidence:
        raise ValidationFailed("a scorecard decision needs at least one evidence reference")
    unknown = set(criteria) - set(CRITERIA)
    if unknown:
        raise ValidationFailed(f"unknown criteria {sorted(unknown)}")
    if decision == "accept" and not all(criteria.get(c) is True for c in CRITERIA):
        raise ValidationFailed("accept requires every criterion true", criteria=list(CRITERIA))
    v.scorecard = {"decision": decision, "criteria": {c: criteria.get(c) for c in CRITERIA}, "evidence": evidence,
                   "notes": notes, "decided_by": principal.username}
    if decision == "reject":
        v.status = "rejected"
    audit.record(db, principal.tenant_id, principal.username, f"vendor.scorecard_{decision}", "vendor", v.id)
    return v


class FileHandoffAdapter:
    def __init__(self, outbox: str):
        self.outbox = Path(outbox)

    def submit(self, vendor: Vendor, job: dict, idempotency_key: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", idempotency_key):
            raise ValidationFailed("idempotency_key must be 1-100 chars of [A-Za-z0-9_.-]")
        folder = self.outbox / vendor.tenant_id / f"vendor-{vendor.id}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{idempotency_key}.json"
        manifest = {"vendor": vendor.name, "format": vendor.handoff_format, "job": job, "idempotency_key": idempotency_key}
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        return {"channel": "file_handoff", "path": str(path), "status": "awaiting_vendor"}


class ApiAdapter:
    def submit(self, vendor: Vendor, job: dict, idempotency_key: str) -> dict:
        raise ValidationFailed(f"no API adapter registered for vendor {vendor.name!r}; use file_handoff or add an adapter",
                               vendor_id=vendor.id)


def handoff(db: Session, principal: Principal, settings, vendor_id: int, job: dict, idempotency_key: str) -> dict:
    v = get_vendor(db, principal.tenant_id, vendor_id)
    if v.status != "qualified":
        raise Conflict(f"vendor {v.name} is {v.status}; only qualified vendors receive jobs")
    adapter = FileHandoffAdapter(settings.vendor_outbox_dir) if v.mode == "file_handoff" else ApiAdapter()
    result = adapter.submit(v, job, idempotency_key)
    audit.record(db, principal.tenant_id, principal.username, "vendor.handoff", "vendor", v.id,
                 {"idempotency_key": idempotency_key, "channel": result["channel"]})
    return result


def qualified_vendors(db: Session, tenant_id: str) -> list[Vendor]:
    return list(db.scalars(select(Vendor).where(Vendor.tenant_id == tenant_id, Vendor.status == "qualified")).all())


def vendor_view(v: Vendor) -> dict:
    return {"id": v.id, "name": v.name, "capability": v.capability, "mode": v.mode, "endpoint": v.endpoint,
            "handoff_format": v.handoff_format, "license_verified": v.license_verified,
            "data_terms_accepted": v.data_terms_accepted, "status": v.status, "scorecard": v.scorecard}
