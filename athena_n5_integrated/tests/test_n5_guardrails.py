"""N5.4 hard rejection matrix through the API: the nine Data Quality Gateway conditions, a cheaper
unauthorized alternate that never wins, and N0 rights revoked mid-workflow.

    BLOCK   MPN differs · obsolete design revision · supplier lacks approval · quote expired ·
            source rights revoked (blocks downstream handoff)
    REWORK  stock insufficient · lead time over target · capacity evidence missing ·
            critical line without an eligible quote
"""
from __future__ import annotations

import pytest

from n5_support import BOURNS, MURATA, by_mpn, codes, offer


@pytest.fixture
def ready(flow5):
    flow5.bom_released()
    flow5.standard_suppliers()
    return flow5, flow5.n5_intake()


def _bourns_ok(flow, intake):
    """An eligible quote for the Bourns line so only the Murata line is under test."""
    assert flow.quote(intake, "SUP-DIST-B", [offer(flow.L[BOURNS], BOURNS, "9.40")], ref="Q-BOURNS").status_code == 201


def _murata(flow, intake, supplier="SUP-DIST-A", price="6.50", **kw):
    r = flow.quote(intake, supplier, [offer(flow.L[MURATA], MURATA, price, **{k: v for k, v in kw.items()
                                                                              if k not in ("valid", "rev")})],
                   **{k: v for k, v in kw.items() if k in ("valid", "rev")})
    assert r.status_code == 201, r.text
    return r.json()


# ----------------------------------------------------------------------------------------------
# BLOCK conditions (offer becomes ineligible whatever it costs)
# ----------------------------------------------------------------------------------------------
def test_1_block_when_the_quoted_mpn_differs_from_the_bom(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], MURATA + "-TR", "5.00")])        # packaging variant
    rep = flow.n5_report(intake)
    m = by_mpn(rep, MURATA)
    assert m["status"] == "UNSOURCED" and "MPN_MISMATCH" in m["offers"][0]["codes"]
    assert "ECO_CANDIDATE" in codes(rep, "INFO")


def test_2_block_when_the_quote_is_for_an_obsolete_design_revision(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    _murata(flow, intake, rev="A")
    m = by_mpn(flow.n5_report(intake), MURATA)
    assert m["offers"][0]["codes"] == ["DESIGN_REVISION_OBSOLETE"]


def test_3_block_when_the_supplier_lacks_approval(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    flow.supplier("SUP-PENDING", approve=False)
    _murata(flow, intake, supplier="SUP-PENDING", price="5.00")
    m = by_mpn(flow.n5_report(intake), MURATA)
    assert m["offers"][0]["codes"] == ["SUPPLIER_NOT_APPROVED"] and m["status"] == "UNSOURCED"


def test_3b_cheaper_unauthorized_alternate_is_rejected_and_the_authorized_offer_wins(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    _murata(flow, intake, supplier="SUP-DIST-A", price="6.50")                          # authorized: ₹142
    _murata(flow, intake, supplier="SUP-BROKER", price="4.10")                          # not a Murata channel
    flow.supplier("SUP-SUSPENDED")
    assert flow.review_supplier("SUP-SUSPENDED", decision="SUSPENDED", who="sq").status_code == 200
    _murata(flow, intake, supplier="SUP-SUSPENDED", price="3.90")                       # suspended supplier
    rep = flow.n5_report(intake)
    m = by_mpn(rep, MURATA)
    top = m["offers"][0]
    assert (top["supplier_id"], top["rank"], top["extended_cost"]) == ("SUP-DIST-A", 1, "142.00")
    bad = {x["supplier_id"]: x for x in m["offers"] if not x["eligible"]}
    assert bad["SUP-BROKER"]["codes"] == ["UNAUTHORIZED_CHANNEL"] and bad["SUP-BROKER"]["rank"] is None
    assert bad["SUP-SUSPENDED"]["codes"] == ["SUPPLIER_NOT_APPROVED"]
    assert rep["decision"] == "PASS" and rep["indicative_total"] == "248.00"      # 142 + (10 × 9.40 + 12 = 106)


def test_4_block_when_the_quote_has_expired(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    q = _murata(flow, intake, valid=0.00002)                                             # expires within ~2 s
    import time
    time.sleep(2.2)
    m = by_mpn(flow.n5_report(intake), MURATA)
    assert q["status"] == "ACCEPTED" and m["offers"][0]["codes"] == ["QUOTE_EXPIRED"]


def test_5_block_when_source_rights_are_revoked_before_the_report(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    flow.c.source("SRC-QUOTES-2", permitted_use=("SOURCING",), publisher="partner portal")
    r = flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], MURATA, "6.00")], source="SRC-QUOTES-2")
    assert r.status_code == 201
    assert flow.c.post("/v1/sources/acme/SRC-QUOTES-2/approve", "rita",
                       json={"decision": "REVOKED", "reason": "portal terms withdrawn"}).status_code == 200
    m = by_mpn(flow.n5_report(intake), MURATA)
    assert m["offers"][0]["codes"] == ["SOURCE_RIGHTS_REVOKED"] and m["status"] == "UNSOURCED"


def test_5b_rights_revoked_after_approval_refuses_the_handoff(flow5):
    intake, rep = flow5.sourced()
    assert flow5.n5_review(rep).status_code == 201
    assert flow5.c.post("/v1/sources/acme/SRC-QUOTES/approve", "rita",
                        json={"decision": "REVOKED", "reason": "supplier withdrew consent"}).status_code == 200
    r = flow5.n5_handoff(rep)
    assert r.status_code == 409 and r.json()["detail"]["code"] in ("REPORT_STALE", "SOURCE_RIGHTS_REVOKED")


def test_5c_rights_revoked_after_handoff_blocks_n6_and_n7(flow5):
    intake, rep, h = flow5.handed_off()
    pk = {x["target"]: x for x in h["packages"]}
    n6 = flow5.c.post("/v1/n6/intake", "co", json={"artifact_id": pk["N6"]["artifact_id"],
                                                   "revision": pk["N6"]["revision"]}).json()
    assert flow5.c.post("/v1/sources/acme/SRC-QUOTES/approve", "rita",
                        json={"decision": "REVOKED", "reason": "supplier withdrew consent"}).status_code == 200
    st = flow5.c.get(f"/v1/n5/subjects/handoff/{h['handoff_id']}/status", "pan").json()
    assert st["status"] == "BLOCKED" and any("SRC-QUOTES" in r for r in st["reasons"])
    view = flow5.c.get(f"/v1/n6/bid-inputs/{n6['intake_id']}", "co").json()
    assert view["status"] == "BLOCKED" and view["events"][-1]["status"] == "BLOCKED"
    r = flow5.c.post("/v1/n7/intake", "fo", json={"artifact_id": pk["N7"]["artifact_id"],
                                                  "revision": pk["N7"]["revision"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "HANDOFF_BLOCKED"
    rel = flow5.c.post("/v1/handoffs/release", "pap", json={"artifact_id": pk["N6"]["artifact_id"],
                                                            "revision": pk["N6"]["revision"], "source_node": "N5",
                                                            "target_nodes": ["N6"]})
    assert rel.status_code == 409 and rel.json()["detail"]["code"] == "SOURCE_RIGHTS_REVOKED"


# ----------------------------------------------------------------------------------------------
# REWORK conditions
# ----------------------------------------------------------------------------------------------
def test_6_rework_when_stock_is_below_the_proposed_order(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    _murata(flow, intake, stock=19)                                                      # Q_order 20
    rep = flow.n5_report(intake)
    assert by_mpn(rep, MURATA)["offers"][0]["codes"] == ["STOCK_INSUFFICIENT"] and rep["decision"] == "REWORK"
    assert "LINE_UNSOURCED" in codes(rep, "REWORK", "LINE")


def test_7_rework_when_lead_time_exceeds_the_project_target(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    _murata(flow, intake, lead=31)                                                       # target 30 days
    m = by_mpn(flow.n5_report(intake), MURATA)
    assert m["offers"][0]["codes"] == ["LEAD_TIME_EXCEEDS_TARGET"]


def test_8_rework_when_factory_capacity_evidence_is_missing(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    _murata(flow, intake, availability="FACTORY_ORDER", stock=None, lead=20)
    m = by_mpn(flow.n5_report(intake), MURATA)
    assert m["offers"][0]["codes"] == ["CAPACITY_EVIDENCE_MISSING"]


def test_8b_ems_capacity_must_cover_the_build(ready):
    flow, intake = ready
    flow.ems_supplier()
    assert flow.ems_quote(intake, capacity=5).status_code == 201                         # build quantity is 10
    rep = flow.n5_report(intake)
    assert all("CAPACITY_EVIDENCE_MISSING" in x["codes"] for ln in rep["lines"] for x in ln["offers"])
    ok = flow.ems_quote(intake, capacity=50)
    assert ok.status_code == 201
    rep = flow.n5_report(intake)
    assert rep["decision"] == "PASS" and {by_mpn(rep, m)["offers"][0]["supplier_id"] for m in (MURATA, BOURNS)} == \
        {"SUP-EMS-1"}
    assert {x["kind"] for x in rep["excluded_items_reported"]} == {"NRE"}               # NRE never in the total


def test_9_rework_when_a_critical_line_has_no_eligible_quote(ready):
    flow, intake = ready
    _murata(flow, intake)                                                                # nothing for Bourns
    rep = flow.n5_report(intake)
    assert "CRITICAL_LINE_UNSOURCED" in codes(rep, "REWORK", "LINE") and rep["indicative_total"] is None
    r = flow.n5_review(rep, quote_hashes=rep["quote_hashes"] or ["0" * 64])
    assert r.status_code == 409 and r.json()["detail"]["code"] in ("REWORK_OPEN", "QUOTE_HASH_MISMATCH")


# ----------------------------------------------------------------------------------------------
# no price override, and report-level BLOCKs
# ----------------------------------------------------------------------------------------------
def test_the_cheapest_offer_never_overrides_a_guardrail(ready):
    flow, intake = ready
    _bourns_ok(flow, intake)
    for sup, kw in (("SUP-BROKER", {}), ("SUP-DIST-A", {"stock": 1}), ("SUP-DIST-B", {"lead": 99})):
        _murata(flow, intake, supplier=sup, price="0.01", **kw)
    _murata(flow, intake, supplier="SUP-DIST-A", price="8.00")
    m = by_mpn(flow.n5_report(intake), MURATA)
    assert m["offers"][0]["unit_price"] == "8.00" and m["offers"][0]["rank"] == 1
    assert sum(1 for x in m["offers"] if x["eligible"]) == 1


def test_production_sourcing_without_n4_verification_is_blocked(ready):
    flow, _ = ready
    intake = flow.n5_intake(purpose="PRODUCTION")
    flow.demo_quotes(intake)
    rep = flow.n5_report(intake)
    assert rep["decision"] == "BLOCK" and "DESIGN_NOT_VERIFIED" in codes(rep, "BLOCK", "REPORT")
    r = flow.n5_review(rep)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BLOCK_NOT_APPROVABLE"


def test_tampered_vault_file_blocks_the_offer(ready):
    flow, intake = ready
    flow.ems_supplier()
    q = flow.ems_quote(intake).json()
    from pathlib import Path
    doc = next(f for f in q["files"] if f["role"] == "QUOTE_DOCUMENT")
    root = Path(flow.c.app.state.settings.storage_root)
    path = root / "quarantine" / "acme" / doc["sha256"]
    path.chmod(0o644)
    path.write_bytes(b"%PDF-1.4\n% altered after intake\n%%EOF\n")
    rep = flow.n5_report(intake)
    assert all("QUOTE_HASH_MISMATCH" in x["codes"] for ln in rep["lines"] for x in ln["offers"])
