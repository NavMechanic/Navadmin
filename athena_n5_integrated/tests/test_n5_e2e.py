"""End to end N0 → N1 → N2 → N3 → N4 → N5 → N6 / N7 on the digital thread.

Real (non-synthetic) TED market evidence enters through N0 into N1, which hands an attested RFQ seed
to N2. N2 writes a PRD with a hazard; N3 designs, verifies and releases the design to N4 and the
engineering BOM to N5. N4 verifies the exact design revision and releases it. N5 locks the BOM for a
PRODUCTION build (which needs that N4 VERIFIED release), qualifies suppliers, takes quotes for every
line, ranks the eligible offers, and a separate approver releases N0-checked drafts to N6 and N7.
"""
from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal

import jsonschema

from conftest import award, crm, tender
from n3_support import ROOT, VENDOR, gen
from n4_support import tc
from n5_support import offer

MANUFACTURERS = sorted({p[5] for p in gen.PARTS})
PRICES = {"LM5146RGYR": "182.40", "BSC093N15NS5ATMA1": "96.10", "SRP1270-220M": "9", "GRM32ER72A475KA88": "6.50",
          "EEH-ZA1V101P": "38.75", "RC0603FR-07100KL": "0.42", "RC0603FR-075K36L": "0.42", "1935161": "21.30"}


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


def _n4_verified(flow) -> dict:
    flow.registries()
    i = flow.intake()
    cases = [tc("E-01", ["T-1"], "BENCH_ATE", "output_voltage", "V", 23.5, 24.5),
             tc("E-02", ["T-2"], "BENCH_ATE", "output_current", "A", 10, None),
             tc("E-03", ["T-3", "T-4"], "ACCREDITED_LAB", "output_voltage", "V", 23.5, 24.5),
             tc("E-04", ["T-1"], "SIM", "vout_min", "V", 23.5, 24.5, sim_corner="all", sim_model_uncertainty="0.05")]
    p = flow.n4_plan(i["intake_id"], cases)
    pid = p["plan_id"]
    flow.pfmea(pid, [{"entry_id": "FM-T", "process_step": "thermal pad", "function": "heat transfer",
                      "failure_mode": "missing thermal pad", "effect": "overheating at 70 °C", "cause": "omission",
                      "detection_controls": [{"description": "70 °C lab run", "test_id": "E-03"}],
                      "severity": 9, "occurrence": 2, "detection": 3, "hazard_ids": ["H-T"]}])
    for tid, cmd, ch, unit, v in (("E-01", "MEAS:VOLT:DC?", "output_voltage", "V", "24.01"),
                                  ("E-02", "MEAS:CURR:DC?", "output_current", "A", "10.2")):
        name, data = flow.scpi_log(tid, [(cmd, v, ch, unit, "0.05")])
        assert flow.post_scpi(pid, name, data).status_code == 201
    pdf = flow.lab_pdf()
    assert flow.post_lab(pid, pdf, flow.lab_manifest(pdf, [flow.lab_result("E-03", "output_voltage", "24.03", "V",
                                                                          "0.08")])).status_code == 201
    a = flow.n4_assess(pid)
    assert a["decision"] == "PASS", [(f["code"], f["message"]) for f in a["findings"] if f["outcome"] != "INFO"]
    assert flow.n4_review(a).status_code == 201
    rel = flow.n4_release(pid)
    assert rel.status_code == 201, rel.text
    return rel.json()


def test_n0_n1_n2_n3_n4_n5_n6_n7_digital_thread(flow5):
    seed = _n1_seed(flow5)
    flow5.c.source("SRC-PRODUCT", permitted_use=("PRODUCT_DEFINITION", "VENDOR_TRANSMISSION"),
                   publisher="N2 product definitions", allowed_vendor_ids=[VENDOR])
    flow5.c.source("SRC-DESIGN", permitted_use=("DESIGN_ENGINEERING", "VERIFICATION", "SOURCING"),
                   publisher="N3 design data")
    flow5.n5_sources()
    hazards = [{"hazard_id": "H-T", "description": "overheating at maximum ambient", "severity": 9,
                "requirement_ids": ["T-4"], "standard_ref": "IEC 62368-1 cl. 6"}]
    b = flow5.designed(hazards=hazards, rfq_id=seed)                       # N3 → N4 design releases
    ver = _n4_verified(flow5)                                              # N4 VERIFIED, released to N8 / N6 / N7
    assert flow5.release(b["baseline_id"], "schematic_pcb", targets=("N5",)).status_code == 201   # N3 → N5 BOM
    flow5.load_lines()

    # N5.1 – N5.2: a PRODUCTION intake needs the N4 verification of this exact revision
    intake = flow5.n5_intake(build=100, target=45, purpose="PRODUCTION",
                             critical=[{"line": flow5.L["LM5146RGYR"], "reason": "controller, single source"}])
    vpk = {x["target"]: x for x in ver["packages"]}
    assert intake["verification_status"] == "VERIFIED" and intake["verification_ref"] == \
        f"{vpk['N8']['artifact_id']}@{vpk['N8']['revision']}"
    assert {ln["mpn"] for ln in intake["lines"]} == set(PRICES) and len(intake["lines"]) == 9   # J1 / J2 share an MPN
    flow5.supplier("SUP-DIST-A", proofs=MANUFACTURERS)
    flow5.supplier("SUP-DIST-B", proofs=MANUFACTURERS)

    # N5.3: two authorized distributors quote every BOM line (offers bind to lines, not just MPNs)
    for sup, markup in (("SUP-DIST-A", Decimal("1")), ("SUP-DIST-B", Decimal("1.05"))):
        offers = [offer(ln["line"], ln["mpn"], str((Decimal(PRICES[ln["mpn"]]) * markup).quantize(Decimal("0.01"))),
                        moq=10 if ln["mpn"].startswith("RC0603") else 1, pack=10 if ln["mpn"].startswith("RC0603") else 1,
                        stock=100_000, lead=7 if sup == "SUP-DIST-A" else 4) for ln in intake["lines"]]
        assert flow5.quote(intake, sup, offers, valid=40).status_code == 201

    # N5.4 – N5.5: deterministic report
    rep = flow5.n5_report(intake)
    assert rep["decision"] == "PASS", [(f["code"], f["message"]) for f in rep["findings"] if f["outcome"] != "INFO"]
    assert all(ln["offers"][0]["supplier_id"] == "SUP-DIST-A" for ln in rep["lines"])
    expected = sum((Decimal(ln["offers"][0]["extended_cost"]) for ln in rep["lines"]), Decimal(0))
    assert Decimal(rep["indicative_total"]) == expected

    # N5.6: separate approver, N0-checked drafts to N6 / N7, consumed downstream
    assert flow5.n5_review(rep).status_code == 201
    h = flow5.n5_handoff(rep)
    assert h.status_code == 201, h.text
    pk = {x["target"]: x for x in h.json()["packages"]}
    art = flow5.c.get(f"/v1/artifacts/{pk['N7']['artifact_id']}", "aud").json()["canonical_artifact"]
    assert art["release_stage"] == "PROTOTYPE" and art["synthetic"] is False
    assert art["payload"]["verification_ref"] == intake["verification_ref"]
    bid = flow5.c.post("/v1/n6/intake", "co", json={"artifact_id": pk["N6"]["artifact_id"],
                                                    "revision": pk["N6"]["revision"]})
    cost = flow5.c.post("/v1/n7/intake", "fo", json={"artifact_id": pk["N7"]["artifact_id"],
                                                     "revision": pk["N7"]["revision"]})
    assert bid.status_code == 201 and cost.status_code == 201, (bid.text, cost.text)
    assert bid.json()["design_verification"] == "VERIFIED" and len(cost.json()["cost_lines"]) == 9

    # one lineage graph from TED market evidence to the N6 / N7 drafts
    graph = flow5.c.get(f"/v1/artifacts/{pk['N6']['artifact_id']}/lineage", "aud").json()
    nodes = {n["id"] for n in graph["nodes"]}
    assert f"artifact:{seed}@A" in nodes and "artifact:PRD-PSU-120W@A" in nodes
    assert "artifact:BOM-PSU-120W-schematic_pcb@B" in nodes and any(n.startswith("artifact:MB-") for n in nodes)
    assert any(n.startswith("n6_bid_input:") for n in nodes)
    assert flow5.c.get("/v1/audit/verify", "aud").json()["audit"]["ok"]


def test_n5_examples_match_their_schemas():
    for example, schema in (("synthetic_n5_quotes.json", "n5_quote_request"),
                            ("synthetic_n5_supplier.json", "n5_supplier_request"),
                            ("synthetic_n5_ems_manifest.json", "n5_ems_quote_manifest")):
        body = json.loads((ROOT / "examples" / example).read_text())
        validator = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / f"{schema}.schema.json")
                                                               .read_text()))
        for item in body if isinstance(body, list) else [body]:
            validator.validate(item)


def test_local_n5_demo_runs_end_to_end():
    out = subprocess.run([sys.executable, str(ROOT / "examples/run_n5_demo.py")], capture_output=True, text=True,
                         timeout=600)
    assert out.returncode == 0, out.stderr[-3000:]
    t = out.stdout
    assert "20 × ₹6.50 + ₹12 = ₹142.00" in t and "10 × ₹9.00 + ₹12 = ₹102.00" in t
    assert "₹244.00 indicative material quote" in t and "10 finished products" in t
    assert "UNAUTHORIZED_CHANNEL" in t and "SOURCE_RIGHTS_REVOKED" in t and "HANDOFF_BLOCKED" in t
