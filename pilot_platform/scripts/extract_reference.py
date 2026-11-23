"""Regenerate app/atlas/data/*.json from the two ATHENA reference workbooks.

    python -I scripts/extract_reference.py ARCHITECTURE.xlsx ATLAS.xlsx app/atlas/data

Reads (values only, no macros):
  * architecture workbook → 'Vendor Connector Registry' (47 connectors), 'Ingestion Methods' (ING-01..16)
  * atlas workbook        → 'VENDOR DUE DILIGENCE' (10 weighted criteria), 'VENDOR MASTER' (for the crosswalk)

The connector → atlas crosswalk is exact-name first, then the reviewed CURATED table below.
Anything unmatched is recorded as not in the atlas rather than guessed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

# Reviewed by hand: registry names that differ from the atlas record name.
CURATED = {
    "CON-007": (["AT-0281"], "CELUS Design Studio is a CELUS product"),
    "CON-011": (["AT-0470"], "Jama Connect is a Jama Software product"),
    "CON-012": (["AT-0352"], "atlas lists the KiCad Project"),
    "CON-013": (["AT-0355"], "atlas lists the Ngspice Project"),
    "CON-014": (["AT-0312"], "Altium 365 is an Altium product"),
    "CON-016": (["AT-0282"], "Flux MCP is Flux's MCP interface (same vendor as CON-008)"),
    "CON-018": (["AT-0382"], "atlas lists the FreeCAD Project"),
    "CON-019": (["AT-0315"], "Ansys Electronics Desktop is in the Ansys Electronics record"),
    "CON-021": (["AT-0309"], "Cadence Allegro is a Cadence Design Systems product"),
    "CON-024": (["AT-0850"], "Keysight instruments are a Keysight Technologies line"),
    "CON-025": (["AT-0849"], "TestStand is a National Instruments product"),
    "CON-039": (["AT-0008"], "Opcenter Intelligence is part of the Siemens Opcenter family"),
    "CON-045": (["AT-1084"], "OpenAI API is an OpenAI product"),
    "CON-047": (["AT-1085"], "Anthropic API is an Anthropic product"),
}
NOT_IN_ATLAS = {
    "CON-006": "government procurement portal, not a vendor record",
    "CON-033": "HubSpot not present in the atlas", "CON-034": "Salesforce not present in the atlas",
    "CON-035": "DocuSign not present in the atlas", "CON-036": "internal ATHENA component",
    "CON-043": "AWS IoT Core not present in the atlas",
}
# Ordered keyword rules: connector ingestion mode → reference ingestion routes (Ingestion Methods sheet)
ROUTE_RULES = [
    (r"graphql", "ING-02"), (r"\bmcp\b", "ING-04"), (r"scpi|visa", "ING-13"), (r"mqtt", "ING-12"),
    (r"webhook", "ING-03"), (r"sftp", "ING-06"), (r"\bplc\b|opc", "ING-11"),
    (r"\brest\b|https api|odata|otlp|\bapi\b", "ING-01"),
    (r"cli|local|script|\bsdk\b|internal python|code", "ING-05"),
    (r"csv", "ING-10"),
    (r"portal|manual|export|download|upload|browser|data exchange|connector|feed|file", "ING-08"),
]
SCHEMA_TO_KIND = {
    "market_opportunity.v1": "MARKET", "product_prd.v1": "PRD", "design_package.v1": "SIM",
    "simulation_report.v1": "SIM", "verification_result.v1": "TEST", "bom_item.v1": "BOM",
    "supplier_quote.v1": "BOM", "sourced_bom.v1": "BOM", "supply_risk.v1": "BOM", "bid_package.v1": "QUOTE",
    "project_pnl.v1": "QUOTE", "lifecycle_event.v1": "BUILD",
}
DD_KEYS = {
    "Legal and product identity": ("legal_identity", True),
    "Commercial rights and sublicensing": ("commercial_rights", True),
    "API / exports / connectors": ("api_exports", False),
    "Model input confidentiality": ("input_confidentiality", True),
    "Unit price predictability": ("price_predictability", False),
    "Technical deliverable acceptance": ("deliverable_acceptance", False),
    "Availability and service level": ("availability_sla", False),
    "India procurement / taxes": ("procurement_tax", False),
    "ERP/PLM/MES compatibility": ("system_compatibility", False),
    "Security and audit trace": ("security_audit", False),
}
OWNER_ROLES = {"ATHENA": ["vendor_lead"], "Engineering": ["engineer"], "Security": ["qa"], "Finance": ["finance"],
               "Technical Lead": ["engineer"], "Program Lead": ["programme_manager"]}


def sheet_rows(wb, name, header_index):
    rows = [r for r in wb[name].iter_rows(values_only=True) if any(c not in (None, "") for c in r)]
    header = [str(h).strip() for h in rows[header_index] if h is not None]
    return [dict(zip(header, r)) for r in rows[header_index + 1:]]


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def routes_for(mode: str) -> list[str]:
    found = []
    for part in re.split(r"[/+]| or |,", mode.lower()):
        for pattern, route in ROUTE_RULES:
            if re.search(pattern, part.strip()) and route not in found:
                found.append(route)
                break
    return found or ["ING-08"]


def main(arch_path, atlas_path, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    arch = load_workbook(arch_path, read_only=True, data_only=True)
    atlas = load_workbook(atlas_path, read_only=True, data_only=True)

    master = sheet_rows(atlas, "VENDOR MASTER", 0)
    by_name = {norm(r["Vendor / Platform"]): r["ID"] for r in master}
    ids = {r["ID"] for r in master}

    connectors = []
    for c in sheet_rows(arch, "Vendor Connector Registry", 2):
        cid = c["Connector ID"]
        parts = [norm(p) for p in re.split(r"/", c["Provider / tool"]) if norm(p)]
        exact = [by_name[p] for p in parts if p in by_name]
        if exact:
            atlas_ids, basis, note = exact, "exact_name", ""
        elif cid in CURATED:
            atlas_ids, note = CURATED[cid]
            basis = "curated"
        else:
            atlas_ids, basis, note = [], "not_in_atlas", NOT_IN_ATLAS.get(cid, "no atlas record found")
        assert all(a in ids for a in atlas_ids), (cid, atlas_ids)
        schemas = [s.strip() for s in str(c["Canonical ATHENA mapping"]).split(",") if s.strip()]
        connectors.append({
            "connector_id": cid, "node": c["Node"], "provider": c["Provider / tool"],
            "capability": c["Specialist capability"], "deliverable": c["Expected deliverable"],
            "ingestion_mode": c["Proposed ingestion mode"], "ingestion_routes": routes_for(c["Proposed ingestion mode"]),
            "evidence_status": c["Evidence status"], "doc_url": c["Official doc / provider URL"] or "",
            "auth_license": c["Authentication / license"], "input_type": c["Expected input type"],
            "output_type": c["Expected output type"], "canonical_schemas": schemas,
            "thread_kind": next((SCHEMA_TO_KIND[s] for s in schemas if s in SCHEMA_TO_KIND), None),
            "pricing_basis": c["Pricing basis (not a quotation)"], "gate_fallback": c["Implementation gate / fallback"],
            "reference_state": c["Connector state"], "priority": c["Priority"],
            "internal": cid in NOT_IN_ATLAS and "internal" in NOT_IN_ATLAS[cid],
            "atlas_ids": atlas_ids, "atlas_match_basis": basis, "atlas_match_note": note,
        })
    assert len(connectors) == 47, len(connectors)

    routes = [{
        "route_id": r["Route ID"], "mechanism": r["Ingress mechanism"], "trigger": r["Trigger"],
        "data_types": r["Accepted data type"], "auth": r["Authentication / access"], "prechecks": r["Security / prechecks"],
        "normalization": r["Parsing & normalization"], "acceptance": r["Acceptance validation"],
        "provenance": r["Immutable storage / provenance"], "failure_policy": r["Failure and retry policy"],
        "nodes": [n.strip() for n in str(r["Nodes"]).split(",")], "caution": r["Design caution"],
    } for r in sheet_rows(arch, "Ingestion Methods", 2)]

    criteria = []
    for r in sheet_rows(atlas, "VENDOR DUE DILIGENCE", 0):
        if r.get("Criterion") not in DD_KEYS:
            continue
        key, knockout = DD_KEYS[r["Criterion"]]
        criteria.append({"key": key, "criterion": r["Criterion"], "evidence_requested": r["Validation evidence requested"],
                         "pass_requirement": r["Pass requirement"], "evidence_owner": r["Evidence owner"],
                         "assessor_roles": OWNER_ROLES[r["Evidence owner"]], "weight_pct": int(r["Weight %"]),
                         "risk_if_failed": r["ATHENA risk implication"], "knockout": knockout})
    assert len(criteria) == 10 and sum(c["weight_pct"] for c in criteria) == 100

    for name, payload in (("connectors.json", connectors), ("ingestion_routes.json", routes), ("due_diligence.json", criteria)):
        (out / name).write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    summary = {b: sum(1 for c in connectors if c["atlas_match_basis"] == b) for b in ("exact_name", "curated", "not_in_atlas")}
    print(f"connectors={len(connectors)} {summary} routes={len(routes)} criteria={len(criteria)}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
