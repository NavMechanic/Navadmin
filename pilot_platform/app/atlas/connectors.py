"""Vendor Connector Registry (47 reference connectors) and per-tenant activation.

Every connector starts in `reference` state, matching the registry's own wording
("entitlement not tested", "qualification pending"). Activation (gate G4) needs:
  1. entitlement verified with evidence (contract/plan, API docs version, test call), and
  2. for third-party connectors: a linked atlas record that passed due diligence and a
     qualified engagement vendor bound to this connector.
Activation issues the webhook secret used to sign vendor callbacks; it is shown once.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.errors import Conflict, Forbidden, GateBlocked, NotFound, ValidationFailed
from ..core.security import Principal
from ..models import AtlasVendor, Vendor, VendorConnector
from . import service

DATA = Path(__file__).parent / "data"
REFERENCE: list[dict] = json.loads((DATA / "connectors.json").read_text())
ROUTES: dict[str, dict] = {r["route_id"]: r for r in json.loads((DATA / "ingestion_routes.json").read_text())}
FILE_ROUTES = {"ING-06", "ING-08", "ING-10", "ING-14"}


def seed_connectors(db: Session, tenant_id: str) -> int:
    existing = {c.connector_id: c for c in db.scalars(select(VendorConnector).where(VendorConnector.tenant_id == tenant_id)).all()}
    added = 0
    for ref in REFERENCE:
        if ref["connector_id"] in existing:
            existing[ref["connector_id"]].reference = ref  # refresh reference text, keep activation state
        else:
            db.add(VendorConnector(tenant_id=tenant_id, connector_id=ref["connector_id"], reference=ref))
            added += 1
    db.flush()
    return added


def get_connector(db: Session, tenant_id: str, connector_id: str) -> VendorConnector:
    c = db.scalars(select(VendorConnector).where(VendorConnector.tenant_id == tenant_id,
                                                 VendorConnector.connector_id == connector_id)).first()
    if c is None:
        raise NotFound(f"connector {connector_id} not found")
    return c


def engagement_vendor(db: Session, tenant_id: str, connector_id: str) -> Vendor | None:
    return db.scalars(select(Vendor).where(Vendor.tenant_id == tenant_id, Vendor.connector_id == connector_id,
                                           Vendor.status == "qualified").order_by(Vendor.id)).first()


def readiness(db: Session, tenant_id: str, c: VendorConnector) -> dict:
    ref = c.reference
    atlas = db.scalars(select(AtlasVendor).where(AtlasVendor.tenant_id == tenant_id,
                                                 AtlasVendor.atlas_id.in_(ref["atlas_ids"]))).all() if ref["atlas_ids"] else []
    records = [{"atlas_id": a.atlas_id, "name": a.name, "qualified": service.is_qualified(db, tenant_id, a.id)} for a in atlas]
    vendor = engagement_vendor(db, tenant_id, c.connector_id)
    blockers = []
    if c.state != "active":
        blockers.append(f"connector state is {c.state}")
    if not ref["internal"]:
        if not ref["atlas_ids"]:
            blockers.append("provider is not in the vendor atlas; add it and complete due diligence")
        elif not atlas:
            blockers.append("linked atlas records are not imported for this tenant")
        elif not any(r["qualified"] for r in records):
            blockers.append("no linked atlas record has passed due diligence")
        if vendor is None:
            blockers.append("no qualified engagement vendor is bound to this connector")
    return {"connector_id": c.connector_id, "state": c.state, "internal": ref["internal"], "atlas_records": records,
            "engagement_vendor_id": vendor.id if vendor else None,
            "file_handoff_capable": bool(FILE_ROUTES & set(ref["ingestion_routes"])),
            "produces_thread_artifacts": ref["thread_kind"] is not None, "blockers": blockers}


def connector_view(db: Session, tenant_id: str, c: VendorConnector, full: bool = False) -> dict:
    ref = c.reference
    out = {k: ref[k] for k in ("connector_id", "node", "provider", "capability", "deliverable", "ingestion_mode",
                               "ingestion_routes", "evidence_status", "canonical_schemas", "thread_kind", "priority",
                               "reference_state", "atlas_ids", "atlas_match_basis")}
    out["readiness"] = readiness(db, tenant_id, c)
    if full:
        out.update({k: ref[k] for k in ("doc_url", "auth_license", "input_type", "output_type", "pricing_basis",
                                        "gate_fallback", "atlas_match_note")})
        out["routes"] = [ROUTES[r] for r in ref["ingestion_routes"] if r in ROUTES]
        out["entitlement_evidence"] = c.entitlement_evidence
    return out


def verify_entitlement(db: Session, principal: Principal, connector_id: str, evidence: list[str]) -> VendorConnector:
    if not principal.has_any("vendor_lead", "engineer", "programme_manager"):
        raise Forbidden("entitlement verification needs vendor_lead, engineer or programme_manager")
    evidence = [str(e).strip() for e in evidence if str(e).strip()]
    if not evidence:
        raise ValidationFailed("entitlement needs evidence (contract/plan, API docs version, test call record)")
    c = get_connector(db, principal.tenant_id, connector_id)
    if c.state == "active":
        raise Conflict(f"{connector_id} is already active")
    c.entitlement_evidence = c.entitlement_evidence + [{"by": principal.username, "evidence": evidence}]
    c.state, c.changed_by = "entitlement_verified", principal.username
    audit.record(db, principal.tenant_id, principal.username, "connector.entitlement_verified", "connector", connector_id)
    return c


def activate(db: Session, principal: Principal, connector_id: str) -> tuple[VendorConnector, str]:
    if not principal.has_any("programme_manager", "procurement"):
        raise Forbidden("activation (gate G4) needs programme_manager or procurement")
    c = get_connector(db, principal.tenant_id, connector_id)
    if c.state not in ("entitlement_verified", "suspended") or not c.entitlement_evidence:
        raise GateBlocked(f"{connector_id} needs verified entitlement before activation", state=c.state)
    pending = [b for b in readiness(db, principal.tenant_id, c)["blockers"] if not b.startswith("connector state")]
    if pending:
        raise GateBlocked(f"{connector_id} cannot be activated", blockers=pending)
    secret = secrets.token_urlsafe(32)
    c.state, c.webhook_secret, c.changed_by = "active", secret, principal.username
    audit.record(db, principal.tenant_id, principal.username, "connector.activated", "connector", connector_id)
    return c, secret


def suspend(db: Session, principal: Principal, connector_id: str, reason: str) -> VendorConnector:
    if not principal.has_any("programme_manager", "procurement", "vendor_lead"):
        raise Forbidden("suspension needs programme_manager, procurement or vendor_lead")
    c = get_connector(db, principal.tenant_id, connector_id)
    c.state, c.webhook_secret, c.changed_by = "suspended", None, principal.username
    audit.record(db, principal.tenant_id, principal.username, "connector.suspended", "connector", connector_id, {"reason": reason})
    return c
