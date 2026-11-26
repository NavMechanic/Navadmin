"""Helpers for the N2 / N3 tests: the fictional PSU-120W programme from RFQ to N4 / N5 release.

All design data is synthetic (``examples/designs``). The fake ``kicad-cli`` and ``ngspice`` in
``tests/fixtures/bin`` stand in for the real tools; live-tool tests skip when those are missing.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Optional

from app.n3 import parsers
from app.n3.config import N3Settings
from app.rules import canonical_json, sha256_hex
from app.scanning import EICAR

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / "examples" / "designs"
FAKE_BIN = ROOT / "tests" / "fixtures" / "bin"

_spec = importlib.util.spec_from_file_location("gen_designs", DESIGNS / "gen_designs.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

VENDOR = "VEN-PCBWORKS"
CONSTRAINTS = {"input_voltage_min_v": 43.2, "input_voltage_max_v": 52.8, "ambient_min_c": -40, "ambient_max_c": 85,
               "load_max_a": 5, "load_max_w": 120, "derating_factor": 0.8, "min_clearance_mm": 0.2,
               "min_creepage_mm": 1.5, "thermal_margin_c": 10, "max_pcb_layers": 4}
TEST_LIMITS = [{"parameter": "vout_min", "min": 23.76, "unit": "V", "requirement_ref": "R-002"},
               {"parameter": "vout_max", "max": 24.24, "unit": "V", "requirement_ref": "R-002"}]
SCOPES = ["schematic_pcb", "circuit_simulation", "mechanical_thermal"]
EICAR_BOM = b'"Refs","Qty","MPN"\n"R1","1","X"\n' + EICAR + b"\n"   # AV test signature, not malware
MANIFEST_TOOL_VERSIONS = {"NETLIST": ("ngspice", "42"), "PROJECT": ("kicad", "8.0"), "GEOMETRY": ("FreeCAD", "0.21.2")}


def n3_settings(tmp_path, **kw: Any) -> N3Settings:
    base = dict(kicad_cli=str(FAKE_BIN / "kicad-cli"), ngspice=str(FAKE_BIN / "ngspice"), jobs_inline=True,
                workroot=str(tmp_path), job_timeout_s=30)
    base.update(kw)
    return N3Settings(**base)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def design_files(**over: Any) -> dict[str, bytes]:
    """The base package; ``over`` replaces / adds files (bytes or str), ``None`` removes one."""
    files: dict[str, bytes] = {
        "psu.kicad_sch": gen.schematic().encode(), "psu.kicad_pcb": gen.board().encode(),
        "psu.kicad_pro": gen.project().encode(), "psu_bom.csv": gen.bom_csv().encode(),
        "psu_buck.cir": gen.NETLIST.encode(), "thermal_report.json": json.dumps(gen.THERMAL).encode(),
        "enclosure.step": gen.step().encode(), "psu_datasheet.pdf": gen.PDF, "layout_screenshot.png": gen.PNG}
    for k, v in over.items():
        if v is None:
            files.pop(k, None)
        else:
            files[k] = v.encode() if isinstance(v, str) else v
    return files


PACKAGE_FILES = {
    "ELECTRICAL_SCHEMATIC": ["psu.kicad_sch", "psu.kicad_pro"],
    "PCB_LAYOUT": ["psu.kicad_pcb", "psu.kicad_sch", "psu.kicad_pro", "psu_bom.csv"],
    "DFM": ["psu.kicad_pcb", "psu.kicad_pro"],
    "CIRCUIT_SIMULATION": ["psu_buck.cir", "psu_bom.csv"],
    "MECHANICAL": ["enclosure.step"],
    "THERMAL": ["enclosure.step", "thermal_report.json"],
}
PACKAGE_JOBS = {
    "ELECTRICAL_SCHEMATIC": [("kicad-cli", "sch_erc", ["psu.kicad_sch", "psu.kicad_pro"]),
                             ("kicad-cli", "sch_export_bom", ["psu.kicad_sch", "psu.kicad_pro"])],
    "PCB_LAYOUT": [("kicad-cli", "pcb_drc", ["psu.kicad_pcb", "psu.kicad_sch", "psu.kicad_pro"])],
    "DFM": [("kicad-cli", "pcb_drc", ["psu.kicad_pcb", "psu.kicad_pro"]),
            ("kicad-cli", "pcb_export_gerbers", ["psu.kicad_pcb", "psu.kicad_pro"]),
            ("kicad-cli", "pcb_export_drill", ["psu.kicad_pcb", "psu.kicad_pro"])],
    "CIRCUIT_SIMULATION": [("ngspice", "spice_corners", ["psu_buck.cir"])],
    "MECHANICAL": [],
    "THERMAL": [],
}
KIND_TOOL = {"ELECTRICAL_SCHEMATIC": "kicad-cli", "PCB_LAYOUT": "kicad-cli", "DFM": "kicad-cli",
             "CIRCUIT_SIMULATION": "ngspice"}


def manifest(files: dict[str, bytes], revision: str = "B", replaces: bool = True, **role_over: str) -> dict:
    entries = []
    for name, data in files.items():
        role = role_over.get(name) or parsers.detect(name, data).role
        tool, version = MANIFEST_TOOL_VERSIONS.get(role, (None, None))
        entries.append({"path": name, "role": role, "sha256": sha(data), "tool": tool, "tool_version": version})
    return {"design_revision": revision, "entries": entries, "replaces_previous": replaces}


def rfq(artifact_id: str = "RFQ-PSU-120W", revision: str = "A", synthetic: bool = True) -> dict:
    payload = copy.deepcopy(_RFQ_PAYLOAD)
    payload["rfq_id"] = artifact_id
    return {"tenant_id": "acme", "project_id": "PRJ-PSU-120W", "artifact_id": artifact_id, "artifact_type": "RFQ",
            "source_id": "SRC-RFQ", "processing_purpose": "RFQ_ANALYSIS", "release_stage": "CONCEPT",
            "revision": revision, "synthetic": synthetic, "source_sha256": sha256_hex(canonical_json(payload)),
            "payload": payload}


_RFQ_PAYLOAD = {
    "rfq_id": "RFQ-PSU-120W", "customer_name": "Acme Industrial Controls", "product_category": "power_supply",
    "quantity": 2500, "target_unit_price": {"amount": "18.50", "currency": "USD"},
    "requested_delivery_date": "2027-03-31", "compliance_standards": ["IEC 62368-1"],
    "requirements": [
        {"req_id": "R-001", "parameter": "input_voltage_v", "value": 48, "unit": "V", "value_origin": "CUSTOMER",
         "tolerance": {"kind": "PCT", "plus": 10, "minus": 10}},
        {"req_id": "R-002", "parameter": "output_voltage_v", "value": 24, "unit": "V", "value_origin": "CUSTOMER",
         "tolerance": {"kind": "PCT", "plus": 1, "minus": 1}},
        {"req_id": "R-003", "parameter": "output_current_a", "value": 5, "unit": "A", "value_origin": "CUSTOMER"},
        {"req_id": "R-004", "parameter": "output_power_w", "value": 120, "unit": "W", "value_origin": "CUSTOMER"},
        {"req_id": "R-006", "parameter": "operating_temp_min_c", "value": -40, "unit": "degC",
         "value_origin": "CUSTOMER"},
        {"req_id": "R-007", "parameter": "operating_temp_max_c", "value": 85, "unit": "degC",
         "value_origin": "CUSTOMER"}]}


class Flow:
    """Drives the programme through the API. ``c`` is the conftest ``N1`` client wrapper."""

    def __init__(self, c) -> None:
        self.c = c

    # ------------------------------------------------------------------ N0 / N2 set-up
    def sources(self) -> "Flow":
        self.c.source("SRC-RFQ", permitted_use=("RFQ_ANALYSIS", "PRODUCT_DEFINITION"), publisher="Acme RFQ portal")
        self.c.source("SRC-PRODUCT", permitted_use=("PRODUCT_DEFINITION", "VENDOR_TRANSMISSION"),
                      publisher="ATHENA N2 product definitions", allowed_vendor_ids=[VENDOR])
        self.c.source("SRC-DESIGN", permitted_use=("DESIGN_ENGINEERING", "VERIFICATION", "SOURCING"),
                      publisher="ATHENA N3 design data")
        return self

    def rfq_to_n2(self, artifact: Optional[dict] = None) -> dict:
        art = artifact or rfq()
        r = self.c.post("/v1/gateway/evaluate", "ana", json={"artifact": art, "origin_node": "N0"})
        assert r.status_code == 200 and r.json()["decision"]["decision"] == "PASS", r.text
        ap = self.c.post("/v1/approvals", "apr", json={"artifact_id": art["artifact_id"],
                                                       "exact_revision": art["revision"], "scope": "RELEASE",
                                                       "decision": "APPROVED", "reason": "RFQ cleared for N2"})
        assert ap.status_code == 200, ap.text
        h = self.c.post("/v1/handoffs/release", "apr", json={
            "artifact_id": art["artifact_id"], "revision": art["revision"], "source_node": "N0",
            "target_nodes": ["N2"], "approval_id": ap.json()["approval_event"]["approval_id"]})
        assert h.status_code == 200, h.text
        return art

    def prd_body(self, prd_id: str = "PSU-120W", scopes=None, constraints=None, rfq_id: str = "RFQ-PSU-120W",
                 rfq_rev: str = "A", limits=None) -> dict:
        return {"tenant_id": "acme", "project_id": "PRJ-PSU-120W", "source_id": "SRC-PRODUCT", "prd_id": prd_id,
                "product_name": "PSU-120W DIN-rail 48 V → 24 V", "rfq_artifact_id": rfq_id, "rfq_revision": rfq_rev,
                "design_scopes": scopes or SCOPES, "design_constraints": constraints or CONSTRAINTS,
                "test_limits": TEST_LIMITS if limits is None else limits}

    def prd(self, release: bool = True, **kw: Any) -> dict:
        r = self.c.post("/v1/n2/prds", "pm", json=self.prd_body(**kw))
        assert r.status_code == 200 and r.json()["n0_decision"] == "PASS", r.text
        rec = r.json()
        if release:
            rel = self.c.post(f"/v1/n2/prds/{rec['prd_id']}/revisions/{rec['revision']}/release", "pa",
                              json={"reason": "PRD approved for design"})
            assert rel.status_code == 200, rel.text
            rec["release"] = rel.json()
        return rec

    def ready(self, **kw: Any) -> dict:
        """Sources, RFQ → N2, PRD released to N3, N3 baseline locked."""
        self.sources()
        self.rfq_to_n2()
        self.prd(**kw)
        return self.baseline()

    # ------------------------------------------------------------------ N3
    def baseline(self, prd_id: str = "PSU-120W", who: str = "de", expect: int = 201, **kw: Any) -> dict:
        r = self.c.post("/v1/n3/baselines", who, json={"prd_id": prd_id, "project_id": "PRJ-PSU-120W",
                                                       "design_source_id": "SRC-DESIGN", **kw})
        assert r.status_code == expect, r.text
        return r.json()

    def plan(self, baseline_id: str, scopes=None, amount: str = "20000") -> dict[str, dict]:
        body = {"budget_per_package": {"amount": amount, "currency": "USD"}}
        if scopes:
            body["scopes"] = scopes
        r = self.c.post(f"/v1/n3/baselines/{baseline_id}/plan", "de", json=body)
        assert r.status_code == 201, r.text
        return {w["kind"]: w for w in r.json()}

    def route_tool(self, wp_id: str, tool: str, amount: str = "500"):
        return self.c.post(f"/v1/n3/work-packages/{wp_id}/route", "de", json={
            "executor": "TOOL", "tool": tool, "budget": {"amount": amount, "currency": "USD"}})

    def onboard(self, vendor_id: str = VENDOR, who: str = "da", **kw: Any):
        body = {"vendor_id": vendor_id, "tenant_id": "acme", "name": "PCB Works Ltd (fictional)",
                "capabilities": ["ELECTRICAL_SCHEMATIC", "PCB_LAYOUT", "DFM", "MECHANICAL", "THERMAL"],
                "ip_terms": "ASSIGNED_TO_CUSTOMER", "nda_on_file": True,
                "max_budget": {"amount": "50000", "currency": "USD"}, **kw}
        return self.c.post("/v1/n3/vendors", who, json=body)

    def route_vendor(self, wp_id: str, vendor_id: str = VENDOR, amount: str = "8000"):
        return self.c.post(f"/v1/n3/work-packages/{wp_id}/route", "de", json={
            "executor": "VENDOR", "vendor_id": vendor_id, "budget": {"amount": amount, "currency": "USD"}})

    def deliver(self, wp_id: str, files: dict[str, bytes], who: str = "de", revision: str = "B",
                replaces: bool = True, manifest_body: Optional[dict] = None, **role_over: str):
        m = manifest_body or manifest(files, revision, replaces, **role_over)
        return self.c.post(f"/v1/n3/work-packages/{wp_id}/deliveries", who, data={"manifest": json.dumps(m)},
                           files=[("files", (n, d, "application/octet-stream")) for n, d in files.items()])

    def job(self, wp_id: str, tool: str, operation: str, shas: list[str], who: str = "de", **kw: Any):
        return self.c.post(f"/v1/n3/work-packages/{wp_id}/jobs", who,
                           json={"tool": tool, "operation": operation, "input_sha256": shas, **kw})

    def assess(self, wp_id: str, who: str = "de") -> dict:
        r = self.c.post(f"/v1/n3/work-packages/{wp_id}/assess", who)
        assert r.status_code == 201, r.text
        return r.json()

    def review(self, assessment_id: str, who: str = "da", decision: str = "APPROVED"):
        return self.c.post("/v1/n3/reviews", who, json={"assessment_id": assessment_id, "decision": decision,
                                                        "reason": "design evidence checked by the design authority"})

    def release(self, baseline_id: str, scope: str, targets=("N4", "N5"), who: str = "da"):
        return self.c.post(f"/v1/n3/baselines/{baseline_id}/releases", who,
                           json={"scope": scope, "targets": list(targets)})

    def work_package(self, wp: dict, files: Optional[dict[str, bytes]] = None, deliverer: str = "de",
                     run_jobs: bool = True, route: bool = True) -> dict:
        """Deliver the standard files for a package, run its jobs and assess it."""
        kind, wp_id = wp["kind"], wp["work_package_id"]
        allf = files or design_files()
        pkg = {n: allf[n] for n in PACKAGE_FILES[kind] if n in allf}
        if route and kind in KIND_TOOL:
            assert self.route_tool(wp_id, KIND_TOOL[kind]).status_code == 201
        d = self.deliver(wp_id, pkg, who=deliverer)
        assert d.status_code == 201, d.text
        if run_jobs:
            for tool, op, names in PACKAGE_JOBS[kind]:
                j = self.job(wp_id, tool, op, [sha(pkg[n]) for n in names if n in pkg])
                assert j.status_code == 201 and j.json()["status"] == "SUCCEEDED", j.text
        return self.assess(wp_id)

    def scope(self, baseline_id: str, scope: str, files: Optional[dict[str, bytes]] = None) -> dict[str, dict]:
        """Plan, execute, assess and approve every package of one scope; returns kind → assessment."""
        wps = self.plan(baseline_id, [scope])
        out = {}
        for kind, wp in wps.items():
            a = self.work_package(wp, files)
            out[kind] = a
            if a["decision"] == "PASS":
                r = self.review(a["assessment_id"])
                assert r.status_code == 201, r.text
        return out


def codes(assessment: dict, outcome: Optional[str] = None) -> set[str]:
    return {f["code"] for f in assessment["findings"] if outcome is None or f["outcome"] == outcome}
