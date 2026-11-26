"""N0 release policies for the product / design digital thread: PRD (N2 → N3), DESIGN_RELEASE for all
three design scopes (N3 → N4) and BOM_RELEASE (N3 → N5)."""
from __future__ import annotations

import copy

import pytest

from app.rules import canonical_json, sha256_hex
from app.scanning import sniff_mime, validate_upload
from conftest import codes

SHA = "a" * 64


def _art(aid: str, atype: str, payload: dict, purpose: str) -> dict:
    return {"tenant_id": "acme", "project_id": "PRJ-D", "artifact_id": aid, "artifact_type": atype,
            "source_id": "SRC-ENG", "processing_purpose": purpose, "release_stage": "PROTOTYPE", "revision": "A",
            "synthetic": False, "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload}


def _release(scope: str, **over) -> dict:
    base = {"release_id": "N3R-1", "scope": scope, "prd_ref": "PRD-1@A", "prd_payload_sha256": SHA,
            "design_revision": "B", "assessment_ids": ["N3A-1"], "review_ids": ["N3V-1"],
            "approval_scope": "next controlled engineering step only"}
    if scope == "schematic_pcb":
        base.update(files=[{"path": "psu.kicad_sch", "role": "SCHEMATIC", "sha256": SHA, "tool": "kicad",
                            "tool_version": "8.0.4"},
                           {"path": "psu.kicad_pcb", "role": "PCB", "sha256": SHA, "tool": "kicad",
                            "tool_version": "8.0.4"}],
                    erc={"tool": "kicad-cli", "tool_version": "8.0.4", "critical": 0, "errors": 0, "warnings": 2},
                    drc={"tool": "kicad-cli", "tool_version": "8.0.4", "critical": 0, "errors": 0, "warnings": 0})
    elif scope == "circuit_simulation":
        base.update(files=[{"path": "psu.cir", "role": "NETLIST", "sha256": SHA, "tool": "ngspice",
                            "tool_version": "42"}],
                    simulation={"solver": "ngspice", "solver_version": "42",
                                "corners": [{"name": c, "converged": True} for c in
                                            ("vin_min", "vin_max", "temp_max", "load_max")]})
    else:
        base.update(files=[{"path": "enclosure.step", "role": "GEOMETRY", "sha256": SHA, "tool": "FreeCAD",
                            "tool_version": "0.21"}],
                    thermal={"solver": "Elmer", "solver_version": "9.0", "boundary_conditions": {"h_w_m2k": 10},
                             "ambient_c": 55, "load_w": 18, "max_temps_c": {"Q1": 98}, "limits_c": {"Q1": 125},
                             "margin_c": 10})
    base.update(over)
    return base


@pytest.fixture
def eng(n0):
    n0.source("SRC-ENG", permitted_use=("VERIFICATION", "SOURCING", "PRODUCT_DEFINITION"))
    return n0


@pytest.mark.parametrize("scope", ["schematic_pcb", "circuit_simulation", "mechanical_thermal"])
def test_complete_design_release_passes_and_routes_to_n4(eng, scope):
    ok = eng.evaluate(_art(f"N3R-{scope}", "DESIGN_RELEASE", _release(scope), "VERIFICATION"))
    assert ok["decision"]["decision"] == "PASS", ok["decision"]["explanation"]
    assert ok["decision"]["allowed_destinations"] == ["N4"]


@pytest.mark.parametrize("scope, change, code", [
    ("schematic_pcb", {"files": [{"path": "psu.pdf", "role": "DOCUMENT", "sha256": SHA}]}, "NATIVE_FILES_MISSING"),
    ("schematic_pcb", {"drc": {"tool": "kicad-cli", "critical": 1, "errors": 1, "warnings": 0}}, "CRITICAL_ERC_DRC"),
    ("schematic_pcb", {"erc": None}, "SCOPE_EVIDENCE_MISSING"),
    ("schematic_pcb", {"clearance": [{"kind": "creepage", "required_mm": 6.4, "actual_mm": 4.0}]}, "CREEPAGE_CLEARANCE"),
    ("schematic_pcb", {"derating": [{"refdes": "Q1", "parameter": "vds", "stress": 52, "rating": 60, "factor": 0.8}]},
     "DERATING_BREACH"),
    ("circuit_simulation", {"simulation": {"solver": "ngspice", "solver_version": "42",
                                           "corners": [{"name": "vin_min", "converged": True}]}},
     "WORST_CASE_CORNER_MISSING"),
    ("mechanical_thermal", {"files": [{"path": "render.png", "role": "IMAGE", "sha256": SHA}]}, "NATIVE_GEOMETRY_MISSING"),
    ("mechanical_thermal", {"thermal": {"boundary_conditions": {}, "ambient_c": 55, "load_w": 18,
                                        "max_temps_c": {"Q1": 120}, "limits_c": {"Q1": 125}, "margin_c": 10}},
     "THERMAL_LIMIT_EXCEEDED"),
])
def test_hard_design_policies_block(eng, scope, change, code):
    res = eng.evaluate(_art("N3R-BAD", "DESIGN_RELEASE", _release(scope, **change), "VERIFICATION"))
    assert res["decision"]["decision"] == "BLOCK" and code in codes(res), res["decision"]["explanation"]


@pytest.mark.parametrize("scope, change, code", [
    ("circuit_simulation", {"simulation": {"solver": "ngspice", "corners": [
        {"name": c, "converged": c != "temp_max"} for c in ("vin_min", "vin_max", "temp_max", "load_max")]}},
     {"SOLVER_VERSION_MISSING", "SIMULATION_NOT_CONVERGED"}),
    ("mechanical_thermal", {"thermal": {"boundary_conditions": {}, "max_temps_c": {}}},
     {"THERMAL_BOUNDARY_MISSING", "THERMAL_ASSUMPTIONS_MISSING"}),
])
def test_incomplete_cae_evidence_is_rework(eng, scope, change, code):
    res = eng.evaluate(_art("N3R-RW", "DESIGN_RELEASE", _release(scope, **change), "VERIFICATION"))
    assert res["decision"]["decision"] == "REWORK" and code <= codes(res)


def test_unversioned_native_files_block(eng):
    p = _release("schematic_pcb")
    p["files"][1]["tool_version"] = None
    res = eng.evaluate(_art("N3R-UV", "DESIGN_RELEASE", p, "VERIFICATION"))
    assert "DESIGN_FILE_UNVERIFIED" in codes(res) and res["decision"]["decision"] == "BLOCK"


def _bom(**over) -> dict:
    p = {"release_id": "N3B-1", "scope": "schematic_pcb", "prd_ref": "PRD-1@A", "design_revision": "B",
         "pcb_revision": "B", "bom_source_sha256": SHA, "items": [
             {"line": 1, "mpn": "RC0603FR-0710KL", "manufacturer": "Yageo", "refdes": ["R1", "R2"], "qty": 2},
             {"line": 2, "mpn": "GRM188R71H104KA93D", "manufacturer": "Murata", "refdes": ["C1"], "qty": 1}]}
    p.update(over)
    return p


def test_bom_release_routes_to_n5_and_checks_revision_and_lines(eng):
    ok = eng.evaluate(_art("N3B-OK", "BOM_RELEASE", _bom(), "SOURCING"))
    assert ok["decision"]["decision"] == "PASS" and ok["decision"]["allowed_destinations"] == ["N5"]
    drift = eng.evaluate(_art("N3B-DRIFT", "BOM_RELEASE", _bom(pcb_revision="C"), "SOURCING"))
    assert drift["decision"]["decision"] == "BLOCK" and "BOM_REVISION_MISMATCH" in codes(drift)
    bad = _bom()
    bad["items"][1].update(mpn=None, refdes=["C1", "R2"], qty=1)
    res = eng.evaluate(_art("N3B-BAD", "BOM_RELEASE", bad, "SOURCING"))
    assert {"BOM_LINE_INVALID", "DUPLICATE_REFDES"} <= codes(res) and res["decision"]["decision"] == "REWORK"


def test_eda_files_are_sniffed_from_content():
    assert sniff_mime(b'(kicad_sch (version 20231120) (generator "eeschema")') == "application/x-kicad-schematic"
    assert sniff_mime(b'(kicad_pcb (version 20240108) (generator "pcbnew")') == "application/x-kicad-pcb"
    assert sniff_mime(b"* buck converter\nV1 in 0 DC {vin}\n.tran 1u 1m\n.end\n") == "text/x-spice"
    assert sniff_mime(b"G04 Gerber*\n%FSLAX46Y46*%\n") == "application/vnd.gerber"
    assert validate_upload(b'(kicad_pcb (version 20240108))', "board.kicad_pcb", "text/plain", 10 ** 6).ok
    assert not validate_upload(b'(kicad_pcb (version 20240108))', "board.pdf", None, 10 ** 6).ok
    assert not validate_upload(b"%PDF-1.7 fake", "board.kicad_pcb", None, 10 ** 6).ok


def test_prd_requirements_get_rfq_grade_checks(eng):
    payload = {"prd_id": "PRD-1", "product_name": "24 V DIN-rail PSU", "customer_name": "Example Board",
               "product_category": "power_electronics", "quantity": 800, "authored_by": "pm",
               "design_scopes": ["schematic_pcb"], "design_constraints": {"input_voltage_min_v": 36},
               "requirements": [{"req_id": "R-1", "parameter": "output_voltage_v", "value": 24, "unit": "A",
                                 "value_origin": "CUSTOMER"}]}
    res = eng.evaluate(_art("PRD-1", "PRD", payload, "PRODUCT_DEFINITION"))
    assert res["decision"]["decision"] == "REWORK" and "UNIT_DIMENSION_MISMATCH" in codes(res)
    ok = copy.deepcopy(payload)
    ok["requirements"][0]["unit"] = "V"
    good = eng.evaluate(_art("PRD-2", "PRD", ok, "PRODUCT_DEFINITION"), who="n2", origin_node="N2")
    assert good["decision"]["decision"] == "PASS" and good["decision"]["allowed_destinations"] == ["N3"]
