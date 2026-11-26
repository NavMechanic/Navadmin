"""End to end N0 → N1 → N2 → N3 → N4 → N8 on the digital thread.

Real (non-synthetic) TED market evidence enters through N0 into N1. N1 hands an attested RFQ seed to
N2. N2 writes a PRD with a hazard and releases it to N3. N3 designs, verifies and releases the
schematic/PCB and simulation design revisions to N4. N4 locks the intake, plans, verifies with
calibrated bench, accredited-lab and simulation evidence, a qualified test authority approves the
exact evidence hash, and N0-checked packages reach N8 (genealogy + monitors), N6 and N7."""
from __future__ import annotations

import json
import subprocess
import sys

import jsonschema

from conftest import award, crm, tender
from n3_support import ROOT, VENDOR
from n4_support import days, tc


def _n1_seed(flow) -> str:
    c = flow.c
    c.source("SRC-TED")
    c.source("SRC-N1-DERIVED", permitted_use=("RFQ_ANALYSIS", "COMMERCIAL", "FINANCE"), publisher="N1 derived")
    c.submit_ok(tender(), award())
    r = c.post("/v1/n1/buyers", "ana", json={"tenant_id": "acme", "project_id": "PRJ-MKT-2027", "source_id": "SRC-TED",
                                             "records": {"batch_id": "CRM-B1", "connector": "CRM",
                                                         "retrieved_at": tender()["published_at"], "records": [crm()]}})
    assert r.status_code == 200, r.text
    for eid in ("TED-2026-000101", "TED-2026-000202"):
        assert c.attest(eid).status_code == 200
    rep = c.report()
    h = c.post(f"/v1/n1/reports/{rep['report_id']}/handoff", "apr",
               json={"targets": ["N2"], "derived_source_id": "SRC-N1-DERIVED", "project_id": "PRJ-MKT-2027"})
    assert h.status_code == 200, h.text
    return h.json()["packages"][0]["artifact_id"]


def test_n0_n1_n2_n3_n4_n8_digital_thread(flow4):
    seed = _n1_seed(flow4)
    flow4.c.source("SRC-PRODUCT", permitted_use=("PRODUCT_DEFINITION", "VENDOR_TRANSMISSION"),
                   publisher="N2 product definitions", allowed_vendor_ids=[VENDOR])
    flow4.c.source("SRC-DESIGN", permitted_use=("DESIGN_ENGINEERING", "VERIFICATION", "SOURCING"),
                   publisher="N3 design data")
    hazards = [{"hazard_id": "H-T", "description": "overheating at maximum ambient", "severity": 9,
                "requirement_ids": ["T-4"], "standard_ref": "IEC 62368-1 cl. 6"}]
    flow4.designed(hazards=hazards, rfq_id=seed)
    flow4.registries()
    i = flow4.intake()
    assert i["synthetic"] is False and {r["req_id"] for r in i["requirements"] if r["safety_critical"]} == {"T-4"}
    cases = [tc("E-01", ["T-1"], "BENCH_ATE", "output_voltage", "V", 23.5, 24.5),
             tc("E-02", ["T-2"], "BENCH_ATE", "output_current", "A", 10, None),
             tc("E-03", ["T-3", "T-4"], "ACCREDITED_LAB", "output_voltage", "V", 23.5, 24.5,
                title="operation at −25 °C and +70 °C"),
             tc("E-04", ["T-1"], "SIM", "vout_min", "V", 23.5, 24.5, sim_corner="all", sim_model_uncertainty="0.05")]
    p = flow4.n4_plan(i["intake_id"], cases)
    pid = p["plan_id"]
    assert flow4.c.get(f"/v1/n4/plans/{pid}/traceability", "te").json()["complete"]
    flow4.pfmea(pid, [{"entry_id": "FM-T", "process_step": "thermal pad", "function": "heat transfer",
                       "failure_mode": "missing thermal pad", "effect": "overheating at 70 °C", "cause": "omission",
                       "detection_controls": [{"description": "70 °C lab run", "test_id": "E-03"}],
                       "severity": 9, "occurrence": 2, "detection": 3, "hazard_ids": ["H-T"]}])
    for tid, cmd, ch, unit, v, u in (("E-01", "MEAS:VOLT:DC?", "output_voltage", "V", "24.01", "0.05"),
                                     ("E-02", "MEAS:CURR:DC?", "output_current", "A", "10.2", "0.05")):
        name, data = flow4.scpi_log(tid, [(cmd, v, ch, unit, u)])
        assert flow4.post_scpi(pid, name, data).status_code == 201
    pdf = flow4.lab_pdf()
    res = [flow4.lab_result("E-03", "output_voltage", "24.03", "V", "0.08")]
    assert flow4.post_lab(pid, pdf, flow4.lab_manifest(pdf, res)).status_code == 201
    a = flow4.n4_assess(pid)
    assert a["decision"] == "PASS", [(f["code"], f["message"]) for f in a["findings"] if f["outcome"] != "INFO"]
    assert flow4.n4_review(a).status_code == 201
    rel = flow4.n4_release(pid)
    assert rel.status_code == 201, rel.text
    pk = {x["target"]: x for x in rel.json()["packages"]}

    # the verification package is design-revision locked and N0-checked
    ver = flow4.c.get(f"/v1/artifacts/{pk['N8']['artifact_id']}", "aud").json()
    art = ver["canonical_artifact"]
    assert art["release_stage"] == "PROTOTYPE" and art["synthetic"] is False
    assert [m["target_nodes"] for m in ver["releases"] if m["status"] == "RELEASED"] == [["N8"]]
    assert set(art["payload"]["design_refs"]) == {d["artifact"] for d in i["design_refs"]}

    # N8 receives genealogy and monitors only from that released package
    n8 = flow4.c.post("/v1/n8/intake", "lo", json={"artifact_id": pk["N8"]["artifact_id"],
                                                   "revision": pk["N8"]["revision"]})
    assert n8.status_code == 201, n8.text
    unit = flow4.c.get("/v1/n8/units/PSU-0001", "lo").json()["history"][-1]
    assert unit["design_revision"] == "B" and unit["tests_passed"] == ["E-01", "E-02", "E-03"]
    assert {m["characteristic"] for m in flow4.c.get("/v1/n8/monitors", "lo").json()} == {"output_voltage",
                                                                                          "output_current"}

    # one lineage graph from TED market evidence to the N8 unit
    graph = flow4.c.get(f"/v1/artifacts/{pk['N8']['artifact_id']}/lineage", "aud").json()
    nodes = {n["id"] for n in graph["nodes"]}
    assert f"artifact:{seed}@A" in nodes and "artifact:PRD-PSU-120W@A" in nodes
    assert any(n.startswith("artifact:MB-") for n in nodes) and "unit:PSU-0001" in nodes
    assert any(n.startswith("artifact:DSN-PSU-120W-schematic_pcb@") for n in nodes)
    assert flow4.c.get("/v1/audit/verify", "aud").json()["audit"]["ok"]
    assert days(0) > days(-1)


def test_synthetic_demo_data_never_reaches_n8(flow4):
    flow4.sources()
    flow4.n4_sources()
    art = flow4.rfq_art()
    art["synthetic"] = True
    flow4.rfq_to_n2(art)
    body = flow4.prd_body(scopes=["circuit_simulation"])
    body["hazards"] = [{"hazard_id": "H-S", "description": "demo hazard", "severity": 9, "requirement_ids": ["R-002"]}]
    flow4.c.post("/v1/n2/prds", "pm", json=body)
    flow4.c.post("/v1/n2/prds/PSU-120W/revisions/A/release", "pa", json={"reason": "demo PRD release"})
    b = flow4.baseline()
    flow4.scope(b["baseline_id"], "circuit_simulation")
    assert flow4.release(b["baseline_id"], "circuit_simulation", targets=("N4",)).status_code == 201
    flow4.registries()
    i = flow4.intake()
    assert i["synthetic"] is True
    p = flow4.n4_plan(i["intake_id"], [tc("T-01", ["R-002"], "BENCH_ATE", "output_voltage", "V", 23.5, 24.5)])
    name, data = flow4.scpi_log("T-01", [("MEAS:VOLT:DC?", "24.00", "output_voltage", "V", "0.05")],
                                claimed="SYNTHETIC")
    assert flow4.post_scpi(p["plan_id"], name, data, source="SRC-SIMDATA").status_code == 201
    a = flow4.n4_assess(p["plan_id"])
    v = a["test_verdicts"][0]["measurements"][0]
    assert v["verdict"] == "PASS" and v["provenance"] == "SYNTHETIC"                # in-limit, but not evidence
    assert a["decision"] == "BLOCK" and "SYNTHETIC_NOT_QUALIFICATION" in {f["code"] for f in a["findings"]}
    assert flow4.c.get(f"/v1/n4/plans/{p['plan_id']}/n8-draft", "te").json()["status"] == "DRAFT_NOT_RELEASED"


def test_n4_examples_match_their_schemas():
    for example, schema in (("synthetic_n4_plan.json", "n4_test_plan_request"),
                            ("synthetic_n4_lab_manifest.json", "n4_lab_report_manifest"),
                            ("synthetic_n4_scpi_log.json", "n4_scpi_log")):
        body = json.loads((ROOT / "examples" / example).read_text())
        jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / f"{schema}.schema.json").read_text())
                                        ).validate(body)


def test_local_n4_demo_runs_end_to_end():
    out = subprocess.run([sys.executable, str(ROOT / "examples/run_n4_demo.py")], capture_output=True, text=True,
                         timeout=600)
    assert out.returncode == 0, out.stderr[-3000:]
    t = out.stdout
    assert "24.00 ± 0.05 V" in t and "INDETERMINATE" in t and "SYNTHETIC_NOT_QUALIFICATION" in t
    assert "DRAFT_NOT_RELEASED" in t and "14 test cases" in t and "CUSTOM_ATTENTION_HEURISTIC" in t
