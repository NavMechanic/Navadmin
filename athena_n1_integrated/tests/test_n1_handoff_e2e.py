"""End-to-end N0 → N1 → N2 / N6 / N7: an attested N1 opportunity becomes a valid, N0-checked N2 RFQ
seed, an N6 shortlist and N7 SAM evidence, each released through N0 with an ApprovalEvent."""
from __future__ import annotations

from conftest import MARKET_USES, award, crm, tender


def _setup(n1):
    n1.source("SRC-TED")
    n1.source("SRC-N1-DERIVED", permitted_use=("RFQ_ANALYSIS", "COMMERCIAL", "FINANCE"),
              publisher="ATHENA N1 derived intelligence")
    n1.submit_ok(tender(), award())
    r = n1.post("/v1/n1/buyers", "ana", json={"tenant_id": "acme", "project_id": "PRJ-MKT-2027", "source_id": "SRC-TED",
                                              "records": {"batch_id": "CRM-B1", "connector": "CRM",
                                                          "retrieved_at": tender()["published_at"],
                                                          "records": [crm()]}})
    assert r.status_code == 200 and r.json()["n0_decision"]["decision"] == "PASS", r.text


def test_attested_opportunity_flows_to_n2_n6_n7_through_n0(n1):
    _setup(n1)
    first = n1.report()
    assert first["sam"]["verified_real_sam"] == "0.00"                     # nothing attested yet
    assert set(first["pending_attestations"]) == {"TED-2026-000202"}
    opp = first["opportunities"][0]
    assert opp["rfq_seed_evidence_id"] is None and any("not reviewer-attested" in b for b in opp["rfq_seed_blockers"])

    for eid in ("TED-2026-000101", "TED-2026-000202"):
        assert n1.attest(eid).status_code == 200
    rep = n1.report()
    assert rep["revision"] == 2 and rep["supersedes_report_id"] == first["report_id"]
    assert rep["sam"]["verified_real_sam"] == "432000.00"                  # 400 000 EUR × 1.08, attested award
    assert rep["sam"]["indicative_spend"] == "1350000.00"                  # tender estimate stays indicative
    opp = rep["opportunities"][0]
    assert opp["rfq_seed_evidence_id"] == "TED-2026-000101" and opp["calibrated"] is False
    assert opp["win_probability"] is None and "NAMED_BUYER_UNVALIDATED" not in opp["risk_flags"]

    # superseded report cannot be handed off; the author cannot approve their own report
    body = {"targets": ["N2", "N6", "N7"], "derived_source_id": "SRC-N1-DERIVED", "project_id": "PRJ-MKT-2027"}
    old = n1.post(f"/v1/n1/reports/{first['report_id']}/handoff", "apr", json=body)
    assert old.status_code == 409 and old.json()["detail"]["code"] == "REPORT_SUPERSEDED"
    assert n1.post(f"/v1/n1/reports/{rep['report_id']}/handoff", "ana", json=body).status_code == 403

    h = n1.post(f"/v1/n1/reports/{rep['report_id']}/handoff", "apr", json=body)
    assert h.status_code == 200, h.text
    pk = {p["target"]: p for p in h.json()["packages"]}
    assert {t: p["status"] for t, p in pk.items()} == {"N2": "RELEASED", "N6": "RELEASED", "N7": "RELEASED"}, pk
    assert pk["N2"]["artifact_type"] == "RFQ" and pk["N2"]["n0_decision"] == "PASS"

    # the N2 seed is a real, N0-checked RFQ artifact released to N2 under an ApprovalEvent
    seed = n1.get(f"/v1/artifacts/{pk['N2']['artifact_id']}", "aud").json()
    assert seed["decision"]["decision"] == "PASS" and seed["canonical_artifact"]["artifact_type"] == "RFQ"
    payload = seed["canonical_artifact"]["payload"]
    assert payload["customer_name"] == "Stadtwerke Beispielstadt GmbH" and payload["quantity"] == 800
    assert {r["value_origin"] for r in payload["requirements"]} >= {"CUSTOMER", "DERIVED"}  # N0 adds V × I
    released = [m for m in seed["releases"] if m["status"] == "RELEASED"]
    assert released and released[0]["target_nodes"] == ["N2"] and released[0]["approval_id"]
    assert seed["canonical_artifact"]["source_artifact_ids"] == ["MB-B-1@A"]

    sam = n1.get(f"/v1/artifacts/{pk['N7']['artifact_id']}", "aud").json()["canonical_artifact"]["payload"]
    assert sam["verified_real_sam"] == "432000.00" and len(sam["verified_items"]) == 1
    assert sam["verified_items"][0]["attestation_id"]
    short = n1.get(f"/v1/artifacts/{pk['N6']['artifact_id']}", "aud").json()["canonical_artifact"]["payload"]
    assert short["entries"][0]["tender_deadlines"][0]["evidence_id"] == "TED-2026-000101"

    # lineage runs from the N0 market batch through N1 to the released N2 artifact
    graph = n1.get(f"/v1/artifacts/{pk['N2']['artifact_id']}/lineage", "aud").json()
    rels = {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}
    assert (f"n1_report:{rep['report_id']}", f"artifact:{pk['N2']['artifact_id']}@A", "HANDOFF_PACKAGE") in rels
    assert any(e["relation"] == "DELIVERED_TO" and e["target"] == "node:N2" for e in graph["edges"])

    # handing off again does not release twice
    again = n1.post(f"/v1/n1/reports/{rep['report_id']}/handoff", "apr", json=body).json()
    assert all("already released" in p["reason"] for p in again["packages"])
    assert n1.get("/v1/audit/verify", "aud").json()["audit"]["ok"]


def test_rights_propagate_into_handoff_packages(n1):
    n1.source("SRC-TED", permitted_use=("MARKET_INTELLIGENCE",))          # no RFQ_ANALYSIS / COMMERCIAL / FINANCE
    n1.source("SRC-N1-DERIVED", permitted_use=("RFQ_ANALYSIS", "COMMERCIAL", "FINANCE"))
    n1.submit_ok(tender(), award())
    for eid in ("TED-2026-000101", "TED-2026-000202"):
        n1.attest(eid)
    rep = n1.report()
    h = n1.post(f"/v1/n1/reports/{rep['report_id']}/handoff", "apr",
                json={"targets": ["N2", "N6", "N7"], "derived_source_id": "SRC-N1-DERIVED",
                      "project_id": "PRJ-MKT-2027"}).json()
    assert {p["status"] for p in h["packages"]} == {"REFUSED"}
    assert any("do not permit RFQ_ANALYSIS" in p["reason"] for p in h["packages"] if p["target"] == "N2")


def test_seed_with_unsafe_requirements_stops_at_n0_review(n1):
    n1.source("SRC-TED")
    n1.source("SRC-N1-DERIVED", permitted_use=("RFQ_ANALYSIS", "COMMERCIAL", "FINANCE"))
    t = tender(compliance_standards=[])
    t["requirements"][0] = {"req_id": "T-1", "parameter": "output_voltage_v", "value": 400, "unit": "V",
                            "value_origin": "CUSTOMER"}
    n1.submit_ok(t)
    n1.attest("TED-2026-000101")
    rep = n1.report()
    h = n1.post(f"/v1/n1/reports/{rep['report_id']}/handoff", "apr",
                json={"targets": ["N2"], "derived_source_id": "SRC-N1-DERIVED", "project_id": "PRJ-MKT-2027"}).json()
    p = h["packages"][0]
    assert p["status"] == "REFUSED" and p["n0_decision"] == "REVIEW" and "SAFETY_REVIEW_REQUIRED" in p["violated_codes"]


def test_synthetic_only_report_is_never_handed_off(n1):
    n1.source("SRC-DEMO", permitted_use=MARKET_USES, data_origin="SYNTHETIC")
    n1.source("SRC-N1-DERIVED", permitted_use=("RFQ_ANALYSIS", "COMMERCIAL", "FINANCE"))
    n1.submit_ok(tender(synthetic=True), award(synthetic=True), source_id="SRC-DEMO", synthetic=True)
    rep = n1.report()
    assert rep["sam"]["verified_real_sam"] == "0.00"
    r = n1.post(f"/v1/n1/reports/{rep['report_id']}/handoff", "apr",
                json={"targets": ["N7"], "derived_source_id": "SRC-N1-DERIVED", "project_id": "PRJ-MKT-2027"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SYNTHETIC_ONLY_REPORT"
