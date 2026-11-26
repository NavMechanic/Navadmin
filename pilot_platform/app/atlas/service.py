"""Atlas queries, outreach tracking, weighted due diligence and promotion to an engagement vendor.

Due diligence follows the atlas's VENDOR DUE DILIGENCE sheet: 10 criteria, weights summing to
100%, each scored 0–5 by the role that owns its evidence. Qualification needs every criterion
assessed with evidence, the knock-out criteria (legal identity, commercial rights, model-input
confidentiality) passed, and a weighted score at or above `vendor_dd_min_score`. Any new
assessment reopens a decided record, so a later failing finding revokes qualification.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.errors import Conflict, Forbidden, GateBlocked, NotFound, ValidationFailed
from ..core.security import Principal
from ..db import utcnow
from ..models import AtlasVendor, DueDiligence, Vendor, VendorConnector
from .importer import DIRECTORY, NUMERIC_PRICE

CRITERIA: list[dict] = json.loads((Path(__file__).parent / "data" / "due_diligence.json").read_text())
BY_KEY = {c["key"]: c for c in CRITERIA}
MANUAL_OUTREACH = ("Uncontacted", "Contacted", "Under NDA", "Evaluating", "Declined")


def get_atlas_vendor(db: Session, tenant_id: str, atlas_id: str) -> AtlasVendor:
    v = db.scalars(select(AtlasVendor).where(AtlasVendor.tenant_id == tenant_id, AtlasVendor.atlas_id == atlas_id)).first()
    if v is None:
        raise NotFound(f"atlas vendor {atlas_id} not found")
    return v


def vendor_view(v: AtlasVendor, dd: DueDiligence | None = None, full: bool = False) -> dict:
    s = v.source
    out = {
        "atlas_id": v.atlas_id, "name": v.name, "parent_entity": v.parent_entity, "lifecycle_segment": v.lifecycle_segment,
        "nodes": v.nodes, "selection": v.selection, "ai_evidence": v.ai_evidence,
        "athena_net_output": s.get("athena_net_output"),
        "price": {"terms": s.get("price_terms"), "billing_basis": s.get("billing_basis"), "evidence": v.price_evidence,
                  "reference_url": s.get("price_url") or None, "numeric_public_entry": v.price_evidence == NUMERIC_PRICE,
                  "caution": "Public entry point only — not an all-in ATHENA cost or a quotation."},
        "outreach_status": v.outreach_status, "in_latest_import": v.in_latest_import,
        "due_diligence": dd.decision if dd else "not_started",
        "qualified": bool(dd and dd.decision == "qualified"),
    }
    if full:
        out["source"] = s
        out["outreach_notes"] = v.outreach_notes
    return out


def search(db: Session, tenant_id: str, q: str | None = None, node: str | None = None, segment: str | None = None,
           selection: str | None = None, price_evidence: str | None = None, ai_evidence: str | None = None,
           include_withdrawn: bool = False, limit: int = 50, offset: int = 0) -> dict:
    stmt = select(AtlasVendor).where(AtlasVendor.tenant_id == tenant_id)
    if not include_withdrawn:
        stmt = stmt.where(AtlasVendor.in_latest_import.is_(True))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(AtlasVendor.name).like(like), func.lower(AtlasVendor.parent_entity).like(like)))
    for col, val in ((AtlasVendor.lifecycle_segment, segment), (AtlasVendor.selection, selection),
                     (AtlasVendor.price_evidence, price_evidence), (AtlasVendor.ai_evidence, ai_evidence)):
        if val:
            stmt = stmt.where(col == val)
    rows = db.scalars(stmt.order_by(AtlasVendor.atlas_id)).all()
    if node:  # JSON list membership, filtered portably in Python
        rows = [r for r in rows if node in r.nodes]
    dds = _dd_map(db, tenant_id, [r.id for r in rows[offset:offset + limit]])
    return {"total": len(rows), "limit": limit, "offset": offset,
            "items": [vendor_view(r, dds.get(r.id)) for r in rows[offset:offset + limit]]}


def _dd_map(db: Session, tenant_id: str, ids: list[int]) -> dict[int, DueDiligence]:
    if not ids:
        return {}
    return {d.atlas_vendor_id: d for d in db.scalars(select(DueDiligence).where(
        DueDiligence.tenant_id == tenant_id, DueDiligence.atlas_vendor_id.in_(ids))).all()}


def facets(db: Session, tenant_id: str) -> dict:
    """Per-segment counts in the same shape as the atlas's CATEGORY ANALYTICS sheet."""
    rows = db.scalars(select(AtlasVendor).where(AtlasVendor.tenant_id == tenant_id, AtlasVendor.in_latest_import.is_(True))).all()
    seg = defaultdict(lambda: {"records": 0, "directory_sourced": 0, "numeric_public_price": 0, "pilot": 0, "node_mappings": set()})
    nodes = Counter()
    for r in rows:
        s = seg[r.lifecycle_segment]
        s["records"] += 1
        s["directory_sourced"] += r.research_category == DIRECTORY
        s["numeric_public_price"] += r.price_evidence == NUMERIC_PRICE
        s["pilot"] += r.selection == "Pilot"
        s["node_mappings"].add(r.source.get("node_mapping", ""))
        nodes.update(r.nodes)
    return {
        "records": len(rows), "provisional_parents": len({r.parent_entity for r in rows}),
        "pilot": sum(r.selection == "Pilot" for r in rows),
        "price_evidence": dict(Counter(r.price_evidence for r in rows)),
        "by_node": {n: nodes[n] for n in sorted(nodes, key=lambda x: int(x[1:]))},
        "segments": [{"segment": k, **{kk: (sorted(vv) if isinstance(vv, set) else vv) for kk, vv in v.items()}}
                     for k, v in sorted(seg.items())],
    }


def set_outreach(db: Session, principal: Principal, atlas_id: str, status: str, note: str) -> AtlasVendor:
    if not principal.has_any("vendor_lead", "procurement", "programme_manager"):
        raise Forbidden("outreach updates need vendor_lead, procurement or programme_manager")
    if status not in MANUAL_OUTREACH:
        raise ValidationFailed(f"status must be one of {MANUAL_OUTREACH}; 'Qualified' is set only by due diligence")
    v = get_atlas_vendor(db, principal.tenant_id, atlas_id)
    old = v.outreach_status
    v.outreach_status = status
    v.outreach_notes = v.outreach_notes + [{"at": utcnow().isoformat(), "by": principal.username, "from": old, "to": status,
                                            "note": note}]
    audit.record(db, principal.tenant_id, principal.username, "atlas.outreach", "atlas_vendor", atlas_id, {"from": old, "to": status})
    return v


# --- due diligence ---------------------------------------------------------------

def get_dd(db: Session, tenant_id: str, v: AtlasVendor, create: bool = False) -> DueDiligence | None:
    dd = db.scalars(select(DueDiligence).where(DueDiligence.tenant_id == tenant_id,
                                               DueDiligence.atlas_vendor_id == v.id)).first()
    if dd is None and create:
        dd = DueDiligence(tenant_id=tenant_id, atlas_vendor_id=v.id, assessments={})
        db.add(dd)
        db.flush()
    return dd


def weighted_score(assessments: dict) -> float:
    return round(sum(assessments.get(c["key"], {}).get("score", 0) * c["weight_pct"] for c in CRITERIA) / 100, 3)


def blockers(dd: DueDiligence | None, min_score: float) -> list[str]:
    a = dd.assessments if dd else {}
    out = [f"{c['key']}: not assessed" for c in CRITERIA if c["key"] not in a]
    out += [f"{c['key']}: knock-out criterion not passed ({c['pass_requirement']})"
            for c in CRITERIA if c["knockout"] and c["key"] in a and not a[c["key"]]["passed"]]
    score = weighted_score(a)
    if score < min_score:
        out.append(f"weighted score {score} below {min_score} / 5")
    return out


def dd_view(dd: DueDiligence | None, min_score: float) -> dict:
    a = dd.assessments if dd else {}
    return {"decision": dd.decision if dd else "not_started", "decided_by": dd.decided_by if dd else None,
            "weighted_score": weighted_score(a), "max_score": 5, "threshold": min_score,
            "blockers": blockers(dd, min_score),
            "criteria": [{**{k: c[k] for k in ("key", "criterion", "weight_pct", "knockout", "pass_requirement",
                                                 "evidence_requested", "evidence_owner", "assessor_roles")},
                          "assessment": a.get(c["key"])} for c in CRITERIA]}


def assess(db: Session, principal: Principal, atlas_id: str, key: str, score: int, passed: bool,
           evidence: list[str], notes: str = "") -> DueDiligence:
    crit = BY_KEY.get(key)
    if crit is None:
        raise NotFound(f"unknown criterion {key!r}")
    if not principal.has_any(*crit["assessor_roles"], "programme_manager"):
        raise Forbidden(f"{key} is assessed by {crit['evidence_owner']} ({', '.join(crit['assessor_roles'])})")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 5:
        raise ValidationFailed("score must be an integer 0-5")
    evidence = [str(e).strip() for e in evidence if str(e).strip()]
    if not evidence:
        raise ValidationFailed("an assessment needs at least one evidence reference")
    v = get_atlas_vendor(db, principal.tenant_id, atlas_id)
    dd = get_dd(db, principal.tenant_id, v, create=True)
    reopened = dd.decision != "open"
    dd.assessments = {**dd.assessments, key: {"score": score, "passed": bool(passed), "evidence": evidence, "notes": notes,
                                              "assessed_by": principal.username, "assessed_at": utcnow().isoformat()}}
    if reopened:
        dd.decision, dd.decided_by = "open", None
        if v.outreach_status in ("Qualified", "Declined"):
            v.outreach_status = "Evaluating"
    audit.record(db, principal.tenant_id, principal.username, "atlas.dd_assessed", "atlas_vendor", atlas_id,
                 {"criterion": key, "score": score, "passed": bool(passed), "reopened": reopened})
    return dd


def decide(db: Session, principal: Principal, atlas_id: str, decision: str, notes: str, min_score: float) -> DueDiligence:
    if not principal.has_any("founder", "programme_manager"):
        raise Forbidden("due-diligence decisions need founder or programme_manager")
    if decision not in ("qualified", "rejected"):
        raise ValidationFailed("decision must be 'qualified' or 'rejected'")
    v = get_atlas_vendor(db, principal.tenant_id, atlas_id)
    dd = get_dd(db, principal.tenant_id, v, create=True)
    if decision == "qualified" and (b := blockers(dd, min_score)):
        raise GateBlocked(f"{v.name} cannot be qualified", blockers=b)
    if decision == "rejected" and not notes.strip():
        raise ValidationFailed("a rejection needs notes")
    dd.decision, dd.decided_by, dd.decision_notes = decision, principal.username, notes
    v.outreach_status = "Qualified" if decision == "qualified" else "Declined"
    audit.record(db, principal.tenant_id, principal.username, f"atlas.dd_{decision}", "atlas_vendor", atlas_id,
                 {"weighted_score": weighted_score(dd.assessments)})
    return dd


def is_qualified(db: Session, tenant_id: str, atlas_vendor_pk: int | None) -> bool:
    if atlas_vendor_pk is None:
        return False
    dd = db.scalars(select(DueDiligence).where(DueDiligence.tenant_id == tenant_id,
                                               DueDiligence.atlas_vendor_id == atlas_vendor_pk)).first()
    return bool(dd and dd.decision == "qualified")


def criterion_passed(db: Session, tenant_id: str, atlas_vendor_pk: int | None, key: str) -> bool:
    dd = db.scalars(select(DueDiligence).where(DueDiligence.tenant_id == tenant_id,
                                               DueDiligence.atlas_vendor_id == atlas_vendor_pk)).first()
    return bool(dd and dd.assessments.get(key, {}).get("passed"))


# --- promotion to an engagement vendor -----------------------------------------------

def promote(db: Session, principal: Principal, atlas_id: str, connector_id: str | None, endpoint: str = "",
            handoff_format: str = "") -> Vendor:
    if not principal.has_any("vendor_lead", "programme_manager"):
        raise Forbidden("promotion needs vendor_lead or programme_manager")
    v = get_atlas_vendor(db, principal.tenant_id, atlas_id)
    if not is_qualified(db, principal.tenant_id, v.id):
        raise GateBlocked(f"{v.name} has not passed due diligence", atlas_id=atlas_id)
    conn = None
    if connector_id:
        conn = db.scalars(select(VendorConnector).where(VendorConnector.tenant_id == principal.tenant_id,
                                                        VendorConnector.connector_id == connector_id)).first()
        if conn is None:
            raise NotFound(f"connector {connector_id} not found")
        if atlas_id not in conn.reference["atlas_ids"]:
            raise ValidationFailed(f"{connector_id} ({conn.reference['provider']}) is not linked to {atlas_id}")
    dup = db.scalars(select(Vendor).where(Vendor.tenant_id == principal.tenant_id, Vendor.atlas_vendor_id == v.id,
                                          Vendor.connector_id == connector_id)).first()
    if dup:
        raise Conflict(f"{v.name} is already engaged as vendor {dup.id}")
    mode = "api" if endpoint else "file_handoff"
    vendor = Vendor(tenant_id=principal.tenant_id, name=v.name, capability=v.lifecycle_segment, mode=mode,
                    endpoint=endpoint, handoff_format=handoff_format or (conn.reference["output_type"] if conn else ""),
                    license_verified=criterion_passed(db, principal.tenant_id, v.id, "commercial_rights"),
                    data_terms_accepted=criterion_passed(db, principal.tenant_id, v.id, "input_confidentiality"),
                    atlas_vendor_id=v.id, connector_id=connector_id)
    db.add(vendor)
    db.flush()
    audit.record(db, principal.tenant_id, principal.username, "atlas.promoted", "vendor", vendor.id,
                 {"atlas_id": atlas_id, "connector_id": connector_id})
    return vendor


def stack_for_nodes(db: Session, tenant_id: str, nodes: list[str]) -> dict:
    """A small per-project vendor stack: reference connectors first, then pilot candidates, per node."""
    from .connectors import readiness

    conns = db.scalars(select(VendorConnector).where(VendorConnector.tenant_id == tenant_id)
                       .order_by(VendorConnector.connector_id)).all()
    atlas = db.scalars(select(AtlasVendor).where(AtlasVendor.tenant_id == tenant_id, AtlasVendor.in_latest_import.is_(True))).all()
    dds = _dd_map(db, tenant_id, [a.id for a in atlas if a.selection == "Pilot"])
    out = {}
    for node in nodes:
        node_conns = [c for c in conns if c.reference["node"] == node]
        out[node] = {
            "connectors": [{"connector_id": c.connector_id, "provider": c.reference["provider"],
                            "priority": c.reference["priority"], "state": c.state,
                            "ready": not readiness(db, tenant_id, c)["blockers"]} for c in node_conns],
            "pilot_candidates": [vendor_view(a, dds.get(a.id)) for a in atlas if a.selection == "Pilot" and node in a.nodes],
            "research_candidates": sum(1 for a in atlas if a.selection == "Research" and node in a.nodes),
        }
    return out
