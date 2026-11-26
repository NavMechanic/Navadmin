import json
from datetime import date
from pathlib import Path

import pytest

from app.ai.extraction import extract_rfq
from app.core.errors import ValidationFailed
from app.nodes import n1_market, n2_product, n3_design, n4_quality, n5_sourcing, n7_finance


# --- N1 -----------------------------------------------------------------------
def test_n1_rejects_unsupported_sam_entries():
    out = n1_market.evaluate_opportunities([
        {"account": "Acme", "annual_value": "120000", "evidence": [{"source": "call-2026-10-02", "summary": "RFQ for 5k units"}]},
        {"account": "", "annual_value": "50000", "evidence": [{"source": "x", "summary": "y"}]},
        {"account": "BigCo", "annual_value": "900000", "basis": "tam_share", "evidence": [{"source": "report", "summary": "1% of TAM"}]},
        {"account": "NoEvidence", "annual_value": "10000", "evidence": []},
    ])
    assert [o["account"] for o in out["sam"]] == ["Acme"] and out["sam_total"] == "120000"
    assert out["rejected_count"] == 3
    ok, problems = n1_market.sam_supported({"sam": [{"account": "Forged", "annual_value": "1", "evidence": []}]})
    assert not ok and "no cited evidence" in problems[0]


def test_n1_api_stores_market_artifact_and_passes_gate_j(client, auth, pid):
    body = {"key": "SAM-2026", "opportunities": [
        {"account": "Acme", "annual_value": "120000", "evidence": [{"source": "call-2026-10-02", "summary": "RFQ"}]}]}
    assert client.post("/nodes/n1/market", json=body, headers=auth("analyst")).status_code == 201
    r = client.post(f"/programmes/{pid}/activities/J/check", json={"evidence": {}}, headers=auth("analyst")).json()
    assert r["passed"] is True


# --- N2 -----------------------------------------------------------------------
def _prd(text):
    return n2_product.build_prd(extract_rfq(text))


def test_n2_power_and_derating_checks():
    base = "Quantity: 10\nOperating temperature: -40 to 105 °C\n"
    over = _prd(base + "Supply voltage: 12 V\nMax current: 2 A\nPower budget: 20 W")
    assert over["blocking"] and "exceeds" in over["blocking"][0]["message"]
    tight = _prd(base + "Supply voltage: 12 V\nMax current: 1.5 A\nPower budget: 20 W")
    checks = {f["check"] for f in tight["findings"]}
    assert not tight["blocking"] and {"power_derating", "temperature_derating"} <= checks
    hv = _prd(base + "Supply voltage: 230 V")
    assert any(f["check"] == "safety" for f in hv["findings"])


def test_n2_traceability():
    prd = _prd("Quantity: 10\nSupply voltage: 5 V\nOperating temperature: 0 to 40 C\nCertifications: CE")
    critical = [r["id"] for r in prd["requirements"] if r["critical"]]
    assert n2_product.traceability(prd, {"tests": []})["uncovered"] == critical
    plan = n4_quality.build_test_plan(prd)
    assert n2_product.traceability(prd, plan)["passed"] is True


# --- N3 -----------------------------------------------------------------------
def test_n3_review_rules_and_thermal_calc():
    pack = {"author": "a", "reviewed_by": "a", "erc_passed": True, "drc_passed": False,
            "simulations": [{"type": "thermal", "assumptions": [], "inputs":
                             {"power_w": 2, "theta_ja_c_per_w": 40, "ambient_c": 60, "tj_max_c": 125}}]}
    out = n3_design.review_design_pack(pack)
    assert out["passed"] is False
    assert {"reviewer must differ from author", "drc_passed is not true"} <= set(out["issues"])
    assert out["simulations"][0]["result"] == {"tj_c": 140.0, "margin_c": -15.0, "passed": False}
    assert n3_design.junction_temperature(0.5, 40, 60) == 80.0


# --- N4 -----------------------------------------------------------------------
def test_n4_blocking_gaps():
    prd = _prd("Quantity: 10\nSupply voltage: 5 V\nOperating temperature: 0 to 40 C")
    rows = n4_quality.pfmea([
        {"failure_mode": "short", "severity": 10, "occurrence": 2, "detection": 2},
        {"failure_mode": "drift", "severity": 7, "occurrence": 6, "detection": 5},
        {"failure_mode": "cosmetic", "severity": 2, "occurrence": 3, "detection": 3},
    ])
    gate = n4_quality.quality_gate(prd, n4_quality.build_test_plan(prd), rows, rpn_threshold=200)
    assert gate["passed"] is False and len(gate["blocking_gaps"]) == 2
    with pytest.raises(ValidationFailed):
        n4_quality.pfmea([{"severity": 11, "occurrence": 1, "detection": 1}])


# --- N5 -----------------------------------------------------------------------
BOM = [{"mpn": "abc 123", "qty_per_unit": 2, "refdes": ["R1", "R2"]}, {"mpn": "ABC123", "qty_per_unit": 1, "refdes": ["R3"]},
       {"mpn": "XYZ", "qty_per_unit": 1}]


def bid(supplier, prices, currency="USD", valid="2026-12-31", lead=10):
    return {"supplier": supplier, "currency": currency, "valid_until": valid, "lead_time_days": lead, "unit_prices": prices}


def test_n5_normalises_and_compares():
    norm = n5_sourcing.normalize_bom(BOM)
    assert [(l["mpn"], l["qty_per_unit"]) for l in norm["lines"]] == [("ABC123", 3), ("XYZ", 1)]
    bids = [bid("A", {"ABC123": "1.00", "XYZ": "2.00"}), bid("B", {"ABC123": "0.90", "XYZ": "2.50"}),
            bid("C", {"ABC123": "0.10"}), bid("D", {"ABC123": "1", "XYZ": "1"}, currency="EUR"),
            bid("E", {"ABC123": "1", "XYZ": "1"}, valid="2026-01-01")]
    out = n5_sourcing.run_sourcing(BOM, bids, "USD", date(2026, 11, 1), build_qty=100)
    assert out["comparable_count"] == 2 and out["gate_passed"] is True
    assert out["best_bid"]["supplier"] == "A" and out["best_bid"]["unit_bom_cost"] == "5.0000"
    reasons = {b["supplier"]: b["reasons"] for b in out["bids"]}
    assert "does not price" in reasons["C"][0] and "currency" in reasons["D"][0] and "expired" in reasons["E"][0]


def test_n5_single_source_needs_justification():
    one = [bid("A", {"ABC123": "1", "XYZ": "1"})]
    assert n5_sourcing.run_sourcing(BOM, one, "USD", date(2026, 11, 1), 10)["gate_passed"] is False
    assert n5_sourcing.run_sourcing(BOM, one, "USD", date(2026, 11, 1), 10,
                                    "sole qualified source for XYZ (customer-mandated)")["gate_passed"] is True


# --- N6 -----------------------------------------------------------------------
def test_n6_no_auto_award_and_segregation(client, auth):
    body = {"rfq_ref": "RFQ-9", "bidder": "DesignHouse", "scope": "bundle", "nodes": ["N2", "N3"], "amount": "18000",
            "lead_time_days": 30}
    bid_id = client.post("/nodes/n6/bids", json=body, headers=auth("pm")).json()["id"]
    assert client.post(f"/nodes/n6/bids/{bid_id}/award", json={"confirm": True}, headers=auth("founder")).status_code == 409
    client.post(f"/nodes/n6/bids/{bid_id}/shortlist", headers=auth("procurement"))
    assert client.post(f"/nodes/n6/bids/{bid_id}/award", json={}, headers=auth("founder")).status_code == 422
    assert client.post(f"/nodes/n6/bids/{bid_id}/award", json={"confirm": True}, headers=auth("pm")).status_code == 403
    assert client.post(f"/nodes/n6/bids/{bid_id}/award", json={"confirm": True}, headers=auth("vendor_lead")).status_code == 403
    r = client.post(f"/nodes/n6/bids/{bid_id}/award", json={"confirm": True}, headers=auth("founder"))
    assert r.status_code == 200 and r.json()["decided_by"] == "founder"
    bad = {**body, "scope": "single_node"}
    assert client.post("/nodes/n6/bids", json=bad, headers=auth("pm")).status_code == 422


# --- N7 -----------------------------------------------------------------------
FIN = {"build_qty": 100, "unit_bom_cost": "20.00", "nre": "1000", "labor_hours": 10, "labor_rate_per_hour": "50",
       "ai_cost": "5", "test_cost_per_unit": "2", "target_margin": "0.25", "annual_finance_rate": "0.12",
       "finance_days": 60, "gst_rate": "0.18"}


def test_n7_known_answer():
    # bom 2000, labour 500, tests 200, ai 5; working capital = 3705 × 0.12 × 60/365 = 73.0849;
    # total 3778.0849; price = total / 0.75 = 5037.4466; GST 18% = 906.7404
    out = n7_finance.cost_model(FIN)["outputs"]
    assert out["working_capital_cost"] == "73.08" and out["total_cost"] == "3778.08"
    assert out["price_ex_gst"] == "5037.45" and out["gst"] == "906.74" and out["price_inc_gst"] == "5944.19"
    assert out["unit_price_ex_gst"] == "50.37" and out["unit_contribution"] == "28.37"
    assert out["gross_margin"] == "1259.36" and out["break_even_units"] == 56


def test_n7_is_reproducible_and_tamper_evident():
    rec = n7_finance.cost_model(FIN)
    assert n7_finance.verify(rec)
    tampered = json.loads(json.dumps(rec))
    tampered["inputs"]["target_margin"] = "0.30"
    assert not n7_finance.verify(tampered)
    for bad in ({**FIN, "target_margin": "1"}, {**FIN, "nre": "-1"}, {**FIN, "build_qty": 0}):
        with pytest.raises(ValidationFailed):
            n7_finance.cost_model(bad)


# --- N8 -----------------------------------------------------------------------
def test_n8_synthetic_never_counted_as_actual(client, auth, pid):
    h = auth("qa")
    r = client.post("/nodes/n8/events", json={"events": [{"event_type": "test", "serial": "SN1", "payload": {"result": "pass"}}]},
                    headers=h)
    assert r.status_code == 422  # is_synthetic must be declared
    client.post("/nodes/n8/events", json={"events": [
        {"event_type": "test", "serial": "SN1", "is_synthetic": False, "payload": {"result": "pass"}},
        {"event_type": "test", "serial": "SN2", "is_synthetic": False, "payload": {"result": "fail"}}]}, headers=h)
    client.post("/nodes/n8/replay", json={"units": 50}, headers=h)
    default = client.get("/nodes/n8/summary", headers=h).json()
    assert default["actual"]["counts"]["test"] == 2 and default["actual"]["first_pass_yield"] == 0.5
    assert "synthetic" not in default
    both = client.get("/nodes/n8/summary?include_synthetic=true", headers=h).json()
    assert both["synthetic"]["counts"]["test"] == 50 and "SYNTHETIC" in both["synthetic"]["label"]
    assert client.post(f"/programmes/{pid}/activities/R/check", json={"evidence": {}}, headers=h).json()["passed"] is True


# --- Vendors (L, T) -------------------------------------------------------------
def test_vendor_qualification_scorecards_and_handoff(client, auth, pid, app):
    vl, qa = auth("vendor_lead"), auth("qa")
    a = client.post("/vendors", json={"name": "SimCo", "mode": "file_handoff", "handoff_format": "STEP+CSV",
                                      "license_verified": True, "data_terms_accepted": True}, headers=vl).json()
    b = client.post("/vendors", json={"name": "ApiCo", "mode": "api", "license_verified": True,
                                      "data_terms_accepted": True}, headers=vl).json()
    r = client.post(f"/vendors/{b['id']}/qualify", headers=vl)
    assert r.status_code == 422 and r.json()["missing"] == ["endpoint"]
    client.post(f"/vendors/{a['id']}/qualify", headers=vl)
    assert client.post(f"/programmes/{pid}/activities/L/check", json={}, headers=vl).json()["passed"] is False

    c = client.post("/vendors", json={"name": "LabCo", "mode": "file_handoff", "handoff_format": "PDF",
                                      "license_verified": True, "data_terms_accepted": True}, headers=vl).json()
    client.post(f"/vendors/{c['id']}/qualify", headers=vl)
    assert client.post(f"/programmes/{pid}/activities/L/check", json={}, headers=vl).json()["passed"] is True

    job = {"job": {"design": "tracker-revB", "files": ["board.step"]}, "idempotency_key": "job-1"}
    first = client.post(f"/vendors/{a['id']}/handoff", json=job, headers=vl).json()
    again = client.post(f"/vendors/{a['id']}/handoff", json=job, headers=vl).json()
    assert first["path"] == again["path"] and Path(first["path"]).exists()
    assert client.post(f"/vendors/{b['id']}/handoff", json=job, headers=vl).status_code == 409  # not qualified

    partial = {"decision": "accept", "criteria": {"output_format_ok": True}, "evidence": ["poc-report.pdf"]}
    assert client.post(f"/vendors/{a['id']}/scorecard", json=partial, headers=qa).status_code == 422
    assert client.post(f"/programmes/{pid}/activities/T/check", json={}, headers=qa).json()["passed"] is False
    full = {"decision": "accept", "evidence": ["poc-report.pdf"],
            "criteria": {"output_format_ok": True, "acceptance_met": True, "ip_rights_confirmed": True, "charges_within_quote": True}}
    for vid in (a["id"], c["id"]):
        assert client.post(f"/vendors/{vid}/scorecard", json=full, headers=qa).status_code == 200
    assert client.post(f"/programmes/{pid}/activities/T/check", json={}, headers=qa).json()["passed"] is True
