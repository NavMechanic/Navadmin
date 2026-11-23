"""Synthetic demo data for the DQ Control Tower (tenant ``demo``). Every record is origin SYNTHETIC."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from .builder import build_manifest
from .gateway import ArtifactSubmission, DQGateway
from .registry import EvidenceRecord

TENANT, PROJECT, PRODUCT = "demo", "AVIATOR-DEMO", "AVIATOR-PCBA"
SUBMITTER, APPROVER = "demo.engineer", "demo.qa-lead"


def _submit(gw: DQGateway, node, artifact_id, rev, schema, payload, sources, evidence, tool, **kw):
    content = json.dumps(payload, sort_keys=True, default=str).encode()
    manifest = build_manifest(registry=gw.registry, project_id=PROJECT, node_id=node, artifact_id=artifact_id,
                              revision=rev, output_schema=schema, content=content, sources=sources,
                              evidence_ids=evidence, rights="APPROVED", classification="CONFIDENTIAL",
                              origin="SYNTHETIC", model_or_tool=tool, **kw)
    return gw.submit(ArtifactSubmission(manifest=manifest, payload=payload, content=content), TENANT, SUBMITTER)


def seed_demo(gw: DQGateway) -> dict:
    reg = gw.registry
    if reg.project(PROJECT) is not None:
        return {"tenant": TENANT, "project": PROJECT, "seeded": False}
    now = datetime.now(timezone.utc)
    reg.register_project(PROJECT, TENANT, [PRODUCT])
    reg.add_evidence(EvidenceRecord(evidence_id="EV-NDA-DEMO", tenant_id=TENANT, project_id=PROJECT, kind="NDA",
                                    signed=True, signed_by="legal", valid_from=now - timedelta(days=30)))
    reg.add_approver(APPROVER, TENANT, ["G1", "G2", "G3", "G4", "G5", "G7"])
    g0 = reg.grant_rights(PROJECT, TENANT, "APPROVED", "EV-NDA-DEMO", approved_by="legal").ref
    units = {"input_voltage": "V", "max_current": "mA"}

    prd = _submit(gw, "N2", "PRD-DEMO", "A", "product_prd.v1",
                  {"product_id": PRODUCT, "specs": {"input_voltage": 24.0, "max_current": 500.0},
                   "requirements": [{"req_id": "REQ-1", "text": "Operate from 24 V", "critical": True,
                                     "verification_ids": ["VER-1"]}]},
                  [g0], ["EV-NDA-DEMO"], "tool:athena-prd-editor@1.0", measurement_units=units)
    bom = _submit(gw, "N5", "BOM-DEMO", "A", "bom_item.v1",
                  {"product_id": PRODUCT, "bom_revision": "A", "avl_mpns": ["NRF52840-QIAA-R"],
                   "lines": [{"mpn": "NRF52840-QIAA-R", "manufacturer": "Nordic", "qty_per_unit": 1}]},
                  [g0], ["EV-NDA-DEMO"], "tool:athena-bom@1.0")
    gw.approve(bom.event_id, APPROVER, TENANT, "demo BOM baseline")
    bom_ref = "BOM-DEMO@A"
    quote = {"supplier_quote_id": "SQ-1", "supplier_id": "VEND-1", "bom_revision": "A", "mpn": "NRF52840-QIAA-R",
             "quoted_quantity": 1000, "unit_price": "310.00", "currency": "INR", "gst_included": False,
             "valid_until": (date.today() - timedelta(days=3)).isoformat(), "min_order_qty": 100, "lead_time_days": 21}
    _submit(gw, "N5", "SQ-DEMO-1", "1", "supplier_quote.v1", quote, [bom_ref], ["EV-NDA-DEMO"], "vendor:luminovo@2026",
            vendor_id="AT-0582", source_revision="A")
    _submit(gw, "N5", "SQ-DEMO-2", "1", "supplier_quote.v1",
            {**quote, "supplier_quote_id": "SQ-2", "mpn": "NRF52833-QDAA-R",
             "valid_until": (date.today() + timedelta(days=30)).isoformat()},
            [bom_ref], ["EV-NDA-DEMO"], "vendor:luminovo@2026", vendor_id="AT-0582", source_revision="A")
    _submit(gw, "N7", "PNL-DEMO", "1", "project_pnl.v1",
            {"currency": "INR", "gst_rate": "0.18", "commitment": "PO",
             "lines": {"bom": "310000", "nre": "150000", "tooling": "40000", "lab": "60000", "finance": "12000"},
             "declared": {"subtotal_ex_gst": "572000.00", "gst": "102960.00", "total_inc_gst": "674960.00"},
             "funding": {"available_working_capital": "250000", "peak_cash_gap": "480000"}},
            [g0], ["EV-NDA-DEMO"], "engine:athena-cost@1.0")
    return {"tenant": TENANT, "project": PROJECT, "seeded": True, "hold_event": prd.event_id, "approver": APPROVER}
