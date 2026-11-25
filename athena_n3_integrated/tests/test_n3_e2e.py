"""End to end N0 → N1 → N2 → N3 → N4 / N5 for all three design scopes.

Real (non-synthetic) TED market evidence enters through N0 into N1. N1 hands an attested RFQ seed to N2
through N0. N2 turns it into a PRD that a product authority releases to N3. N3 locks the baseline,
executes and verifies the design, and the design authority releases N0-checked manifests to N4
(design revision, reports, corners, test limits) and N5 (rev-locked engineering BOM)."""
from __future__ import annotations

import json
import subprocess
import sys

import jsonschema
import pytest

from conftest import award, crm, tender
from n3_support import PACKAGE_FILES, ROOT, SCOPES, VENDOR, design_files, sha


def _n1_rfq_seed(flow) -> str:
    c = flow.c
    c.source("SRC-TED")
    c.source("SRC-N1-DERIVED", permitted_use=("RFQ_ANALYSIS", "COMMERCIAL", "FINANCE"),
             publisher="ATHENA N1 derived intelligence")
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
    pkg = h.json()["packages"][0]
    assert pkg["status"] == "RELEASED" and pkg["artifact_type"] == "RFQ"
    return pkg["artifact_id"]


def _design_sources(flow) -> None:
    flow.c.source("SRC-PRODUCT", permitted_use=("PRODUCT_DEFINITION", "VENDOR_TRANSMISSION"),
                  publisher="ATHENA N2 product definitions", allowed_vendor_ids=[VENDOR])
    flow.c.source("SRC-DESIGN", permitted_use=("DESIGN_ENGINEERING", "VERIFICATION", "SOURCING"),
                  publisher="ATHENA N3 design data")


@pytest.mark.parametrize("scope", SCOPES)
def test_n0_n1_n2_n3_to_n4_n5_for_each_design_scope(flow, scope):
    seed = _n1_rfq_seed(flow)
    _design_sources(flow)
    prd = flow.prd(rfq_id=seed, scopes=[scope])
    assert prd["source_rfq"] == f"{seed}@A"
    b = flow.baseline()
    assert b["synthetic"] is False and b["design_scopes"] == [scope]
    for kind, a in flow.scope(b["baseline_id"], scope).items():
        assert a["decision"] == "PASS", (kind, a["findings"])
    r = flow.release(b["baseline_id"], scope)
    assert r.status_code == 201, r.text
    rel = r.json()
    pk = {p["target"]: p for p in rel["packages"]}
    assert pk["N4"]["status"] == "RELEASED" and pk["N4"]["n0_decision"] == "PASS"

    # N4 receives the native design revision, verification evidence and test-limit baselines
    dsn = flow.c.get(f"/v1/artifacts/{pk['N4']['artifact_id']}", "aud").json()
    art, payload = dsn["canonical_artifact"], dsn["canonical_artifact"]["payload"]
    assert art["artifact_type"] == "DESIGN_RELEASE" and art["release_stage"] == "PROTOTYPE"
    assert art["source_artifact_ids"] == [prd["release"]["n0_artifact_id"] + "@A"]
    assert [m["target_nodes"] for m in dsn["releases"] if m["status"] == "RELEASED"] == [["N4"]]
    assert payload["design_revision"] == "B" and payload["test_limits"] and payload["review_ids"] == rel["review_ids"]
    native = {"schematic_pcb": {"SCHEMATIC", "PCB"}, "circuit_simulation": {"NETLIST"},
              "mechanical_thermal": {"GEOMETRY"}}[scope]
    assert native <= {f["role"] for f in payload["files"]}
    assert all(f["sha256"] and f["tool_version"] for f in payload["files"] if f["role"] in native)
    if scope == "circuit_simulation":
        corners = {c["name"]: c for c in payload["simulation"]["corners"]}
        assert set(corners) == {"vin_min", "vin_max", "temp_max", "load_max"}
        assert all(c["converged"] and c["log_sha256"] for c in corners.values())
        assert corners["vin_max"]["parameters"] == {"vin": 52.8, "iload": 5.0}
        assert payload["derating"] and all(d["stress"] <= d["factor"] * d["rating"] for d in payload["derating"])
    if scope == "mechanical_thermal":
        assert payload["thermal"]["ambient_c"] == 85 and payload["thermal"]["boundary_conditions"]
    if scope == "schematic_pcb":
        assert payload["erc"]["critical"] == payload["drc"]["critical"] == 0

    # N5 receives a design-rev-locked engineering BOM where the scope delivered one
    if scope == "mechanical_thermal":
        assert pk["N5"]["status"] == "NOT_APPLICABLE"
    else:
        bom = flow.c.get(f"/v1/artifacts/{pk['N5']['artifact_id']}", "aud").json()
        assert bom["canonical_artifact"]["artifact_type"] == "BOM_RELEASE"
        assert [m["target_nodes"] for m in bom["releases"] if m["status"] == "RELEASED"] == [["N5"]]
        items = bom["canonical_artifact"]["payload"]["items"]
        assert all(i["mpn"] and i["qty"] == len(i["refdes"]) for i in items)

    # one lineage graph from the market evidence to the released design
    graph = flow.c.get(f"/v1/artifacts/{pk['N4']['artifact_id']}/lineage", "aud").json()
    nodes = {n["id"] for n in graph["nodes"]}
    assert f"artifact:{prd['release']['n0_artifact_id']}@A" in nodes and f"artifact:{seed}@A" in nodes
    assert any(n.startswith("artifact:MB-") for n in nodes)                     # N1 market batch through N0
    assert any(e["relation"] == "DELIVERED_TO" and e["target"] == "node:N4" for e in graph["edges"])
    assert flow.c.get("/v1/audit/verify", "aud").json()["audit"]["ok"]
    fb = flow.c.post("/v1/n3/feedback", "da", json={"target_kind": "RELEASE", "target_id": rel["release_id"],
                                                    "label": "CORRECT", "reason": "N4 bench results agree"})
    assert fb.json()["training_eligible"] is True                               # real data, labelled outcome


def test_vendor_routed_pcb_package_reaches_n4_and_n5(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], ["schematic_pcb"])
    assert flow.onboard().status_code == 201
    pcb = wps["PCB_LAYOUT"]["work_package_id"]
    assert flow.route_vendor(pcb).status_code == 201 and flow.route_tool(pcb, "kicad-cli").status_code == 201
    files = {n: design_files()[n] for n in PACKAGE_FILES["PCB_LAYOUT"]}
    d = flow.deliver(pcb, files, who="ven")
    assert d.status_code == 201 and d.json()["vendor_id"] == VENDOR
    j = flow.job(pcb, "kicad-cli", "pcb_drc", [sha(files[n]) for n in ("psu.kicad_pcb", "psu.kicad_sch",
                                                                         "psu.kicad_pro")])
    assert j.json()["status"] == "SUCCEEDED"
    a = flow.assess(pcb)
    assert a["decision"] == "PASS" and flow.review(a["assessment_id"]).status_code == 201
    for kind in ("ELECTRICAL_SCHEMATIC", "DFM"):
        x = flow.work_package(wps[kind])
        assert flow.review(x["assessment_id"]).status_code == 201
    r = flow.release(b["baseline_id"], "schematic_pcb")
    assert r.status_code == 201 and {p["target"]: p["status"] for p in r.json()["packages"]} == \
        {"N4": "RELEASED", "N5": "RELEASED"}
    graph = flow.c.get("/v1/artifacts/DSN-PSU-120W-schematic_pcb/lineage", "aud").json()
    assert graph["nodes"]


def test_synthetic_examples_match_their_schemas():
    for example, schema in (("synthetic_n3_prd.json", "n2_prd_create_request"),
                            ("synthetic_n3_actions.json", "n3_action_request")):
        body = json.loads((ROOT / "examples" / example).read_text())
        jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / f"{schema}.schema.json").read_text())
                                        ).validate(body)
    assert json.loads((ROOT / "examples/synthetic_n3_rfq.json").read_text())["synthetic"] is True


def test_local_demo_runs_end_to_end():
    out = subprocess.run([sys.executable, str(ROOT / "examples/run_n3_demo.py")], capture_output=True, text=True,
                         timeout=600)
    assert out.returncode == 0, out.stderr[-3000:]
    text = out.stdout
    assert "BLOCK_NOT_WAIVABLE" in text and "BLOCK_NOT_APPROVABLE" in text and "decision still BLOCK" in text
    assert text.count("DESIGN_RELEASE  RELEASED") == 3 and "INVALIDATED" in text
    assert "NOT product safety certification" in text
