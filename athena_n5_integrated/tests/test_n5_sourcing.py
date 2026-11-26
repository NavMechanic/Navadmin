"""N5 baseline (part 1 of 2): BOM ingestion & normalisation, supplier intelligence, the deterministic
sourcing report (the ₹244 indicative material quote), independent review and the N6 / N7 handoff."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import EXCLUDED_COST_CATEGORIES, INDICATIVE_MATERIAL_LABEL
from n5_support import BOURNS, MURATA, OS_ID, by_mpn, codes, days, offer


# ----------------------------------------------------------------------------------------------
# N5.1 BOM ingestion & normalisation
# ----------------------------------------------------------------------------------------------
def test_intake_locks_the_n3_bom_with_exact_mpns_and_q_required(flow5):
    flow5.bom_released()
    i = flow5.n5_intake(build=10)
    assert i["bom_ref"] == "BOM-PSU-120W-schematic_pcb@B" and i["design_revision"] == "B"
    lines = {ln["mpn"]: ln for ln in i["lines"]}
    assert lines[MURATA]["qty_per_unit"] == 2 and lines[MURATA]["q_required"] == 20
    assert lines[BOURNS]["qty_per_unit"] == 1 and lines[BOURNS]["q_required"] == 10
    assert lines[BOURNS]["critical"] and not lines[MURATA]["critical"]
    assert all(ln["uom"] == "EA" for ln in i["lines"]) and len(i["intake_sha256"]) == 64
    assert i["synthetic"] is False and i["verification_status"] == "NOT_VERIFIED"


def test_intake_is_idempotent_and_build_quantity_changes_q_required(flow5):
    flow5.bom_released()
    a, b = flow5.n5_intake(build=10), flow5.n5_intake(build=10)
    c = flow5.n5_intake(build=25)
    assert a["intake_id"] == b["intake_id"] and c["intake_id"] != a["intake_id"]
    assert {ln["mpn"]: ln["q_required"] for ln in c["lines"]} == {MURATA: 50, BOURNS: 25}


def test_intake_needs_a_bom_released_to_n5(flow5):
    flow5.bom_released(targets=("N4",))
    r = flow5.n5_intake(expect=409)
    assert r["detail"]["code"] == "BOM_NOT_RELEASED_TO_N5"


@pytest.mark.parametrize("over, code", [
    ({"critical_lines": [{"line": 99, "reason": "not a line"}]}, "UNKNOWN_LINE"),
    ({"release_source_id": "SRC-QUOTES"}, "RELEASE_SOURCE_NOT_APPROVED"),
])
def test_intake_refuses_bad_requests(flow5, over, code):
    flow5.bom_released()
    expect = 422 if code == "UNKNOWN_LINE" else 409
    r = flow5.n5_intake(expect=expect, **over)
    assert r["detail"]["code"] == code


def test_intake_is_on_the_lineage_of_the_bom_artifact(flow5):
    flow5.bom_released()
    i = flow5.n5_intake()
    g = flow5.c.get("/v1/artifacts/BOM-PSU-120W-schematic_pcb/lineage", "aud").json()
    assert f"n5_intake:{i['intake_id']}" in {n["id"] for n in g["nodes"]}


# ----------------------------------------------------------------------------------------------
# N5.2 supplier intelligence
# ----------------------------------------------------------------------------------------------
def test_supplier_submitter_cannot_qualify_their_own_record(flow5):
    flow5.bom_released()
    flow5.supplier("SUP-X", approve=False, who="sq")
    r = flow5.review_supplier("SUP-X", verified=["Murata"], who="sq")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
    assert flow5.review_supplier("SUP-X", verified=["Murata"], who="sq2").status_code == 200


def test_a_distributor_needs_a_verified_current_channel_proof(flow5):
    flow5.bom_released()
    flow5.supplier("SUP-X", approve=False)
    r = flow5.review_supplier("SUP-X", verified=[])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "QUALIFICATION_INCOMPLETE"
    r = flow5.review_supplier("SUP-X", verified=["Texas Instruments"])           # no proof on the record
    assert r.status_code == 409
    ok = flow5.review_supplier("SUP-X", verified=["Murata"])
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED" and ok.json()["version"] == 2


def test_an_ems_needs_an_open_supply_hub_verified_site(flow5):
    flow5.bom_released()
    body = {"sites": [{"site_id": "SITE-1", "name": "EMS Plant 1", "country": "IN", "os_id": OS_ID}]}
    flow5.supplier("SUP-EMS-X", kind="EMS", proofs=(), approve=False, **body)
    assert flow5.review_supplier("SUP-EMS-X").json()["detail"]["code"] == "QUALIFICATION_INCOMPLETE"
    v = flow5.verify_site("SUP-EMS-X", "SITE-1")
    assert v.status_code == 200 and v.json()["sites"][0]["osh_verified"]
    assert v.json()["sites"][0]["osh_evidence"]["method"] == "CSV_SNAPSHOT"
    assert flow5.review_supplier("SUP-EMS-X").status_code == 200


def test_site_verification_refuses_a_country_mismatch(flow5):
    flow5.bom_released()
    flow5.supplier("SUP-EMS-X", kind="EMS", proofs=(), approve=False,
                   sites=[{"site_id": "SITE-1", "name": "EMS Plant 1", "country": "IN", "os_id": OS_ID}])
    r = flow5.verify_site("SUP-EMS-X", "SITE-1", country="CN")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "FACILITY_MISMATCH"


def test_supplier_revisions_return_to_pending(flow5):
    flow5.bom_released()
    flow5.supplier("SUP-X")
    body = {"supplier_id": "SUP-X", "kind": "DISTRIBUTOR", "legal_name": "SUP-X Pvt Ltd", "country": "IN",
            "registration_ids": {"GSTIN": "29ABCDE1234F1Z5"}}
    r = flow5.c.post("/v1/n5/suppliers/SUP-X/revisions", "pan", json=body)
    assert r.status_code == 201 and r.json()["status"] == "PENDING" and r.json()["version"] == 3


# ----------------------------------------------------------------------------------------------
# N5.4 – N5.5 the deterministic report
# ----------------------------------------------------------------------------------------------
def test_demo_indicative_material_quote_is_244_inr(flow5):
    intake, rep = flow5.sourced()
    m, b = by_mpn(rep, MURATA), by_mpn(rep, BOURNS)
    top_m, top_b = m["offers"][0], b["offers"][0]
    assert (top_m["q_order"], top_m["unit_price"], top_m["extended_cost"]) == (20, "6.50", "142.00")
    assert (top_b["q_order"], top_b["unit_price"], top_b["extended_cost"]) == (10, "9", "102")
    assert rep["indicative_total"] == "244.00" and rep["indicative_unit_cost"] == "24.4000"
    assert rep["indicative_scope"] == INDICATIVE_MATERIAL_LABEL
    assert set(rep["excluded_cost_categories"]) == set(EXCLUDED_COST_CATEGORIES)


def test_eligible_offers_are_ranked_by_extended_cost(flow5):
    intake, rep = flow5.sourced()
    m = by_mpn(rep, MURATA)
    assert [(x["supplier_id"], x["rank"], x["extended_cost"]) for x in m["offers"]] == [
        ("SUP-DIST-A", 1, "142.00"), ("SUP-DIST-B", 2, "148.00")]                       # 20 × 6.80 + 12
    assert m["selected_offer_id"] == m["offers"][0]["offer_id"]


def test_report_hash_binds_quotes_and_lines(flow5):
    intake, rep = flow5.sourced()
    assert len(rep["report_sha256"]) == 64 and len(rep["quote_hashes"]) == 1      # SUP-DIST-A wins both lines
    assert len(rep["all_quote_hashes"]) == 2 and rep["quote_source_ids"] == ["SRC-QUOTES"]
    again = flow5.n5_report(intake)
    assert again["report_sha256"] != rep["report_sha256"] and again["indicative_total"] == rep["indicative_total"]


def test_prototype_build_without_n4_verification_is_only_flagged(flow5):
    intake, rep = flow5.sourced()
    assert "DESIGN_NOT_YET_VERIFIED" in codes(rep, "INFO") and rep["decision"] == "PASS"


# ----------------------------------------------------------------------------------------------
# N5.6 review and handoff
# ----------------------------------------------------------------------------------------------
def test_review_must_quote_the_exact_report_and_quote_hashes(flow5):
    intake, rep = flow5.sourced()
    r = flow5.n5_review(rep, report_sha256="0" * 64)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "REPORT_HASH_MISMATCH"
    r = flow5.n5_review(rep, quote_hashes=["1" * 64])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "QUOTE_HASH_MISMATCH"
    assert flow5.n5_review(rep).status_code == 201


def test_handoff_needs_an_approval(flow5):
    intake, rep = flow5.sourced()
    r = flow5.n5_handoff(rep)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "APPROVAL_REQUIRED"


def test_handoff_releases_n0_checked_drafts_to_n6_and_n7(flow5):
    intake, rep, h = flow5.handed_off()
    pk = {x["target"]: x for x in h["packages"]}
    assert h["status"] == "RELEASED" and set(pk) == {"N6", "N7"}
    assert all(x["n0_decision"] == "PASS" and x["manifest_id"] for x in pk.values())
    art = flow5.c.get(f"/v1/artifacts/{pk['N7']['artifact_id']}", "aud").json()
    payload = art["canonical_artifact"]["payload"]
    assert art["canonical_artifact"]["artifact_type"] == "SOURCING_HANDOFF"
    assert payload["indicative_total"] == "244.00" and payload["indicative_scope"] == INDICATIVE_MATERIAL_LABEL
    assert [m["target_nodes"] for m in art["releases"] if m["status"] == "RELEASED"] == [["N7"]]
    n6 = flow5.c.get(f"/v1/artifacts/{pk['N6']['artifact_id']}", "aud").json()["canonical_artifact"]["payload"]
    assert n6["commercial"]["lead_time_constraints"]["target_lead_time_days"] == 30
    assert {f["supplier_id"] for f in n6["commercial"]["supplier_risk_flags"]} == {"SUP-DIST-A"}


def test_n6_and_n7_ingest_the_released_drafts(flow5):
    intake, rep, h = flow5.handed_off()
    pk = {x["target"]: x for x in h["packages"]}
    b = flow5.c.post("/v1/n6/intake", "co", json={"artifact_id": pk["N6"]["artifact_id"],
                                                  "revision": pk["N6"]["revision"]})
    assert b.status_code == 201, b.text
    assert b.json()["indicative_unit_material_cost"] == "24.4000" and b.json()["status"] == "DRAFT"
    c = flow5.c.post("/v1/n7/intake", "fo", json={"artifact_id": pk["N7"]["artifact_id"],
                                                  "revision": pk["N7"]["revision"]})
    assert c.status_code == 201, c.text
    assert Decimal(c.json()["indicative_total"]) == Decimal("244") and len(c.json()["cost_lines"]) == 2
    assert flow5.c.post("/v1/n6/intake", "co", json={"artifact_id": pk["N7"]["artifact_id"],
                                                     "revision": pk["N7"]["revision"]}).status_code == 409


def test_quote_excluded_charges_go_to_n7_not_into_the_total(flow5):
    flow5.bom_released()
    flow5.standard_suppliers()
    intake = flow5.n5_intake()
    m, b = flow5.L[MURATA], flow5.L[BOURNS]
    flow5.quote(intake, "SUP-DIST-A", [
        offer(m, MURATA, "6.50", excluded_charges=[{"kind": "FREIGHT", "amount": "450", "currency": "INR"}]),
        offer(b, BOURNS, "9", excluded_charges=[{"kind": "GST", "amount": "44", "currency": "INR"}])])
    rep = flow5.n5_report(intake)
    assert rep["indicative_total"] == "244.00"
    assert {x["kind"] for x in rep["excluded_items_reported"]} == {"FREIGHT", "GST"}
    assert all(x["in_indicative_total"] is False for x in rep["excluded_items_reported"])
    assert days(0) < days(1)
