"""N1.4 guardrails: zero synthetic leakage into verified_real_sam, duplicate-spend and stale-evidence
exclusion, corrigendum ordering, malformed values, FX, attachments, attestation and rights."""
from __future__ import annotations

import pytest

from conftest import MARKET_USES, award, days, iso, sam_award, tender


def _feature(rep, eid):
    return next(f for f in rep["evidence_features"] if f["evidence_id"] == eid)


# ------------------------------------------------------------------------------------------------
# synthetic vs verified real SAM
# ------------------------------------------------------------------------------------------------
def test_synthetic_batch_reports_hypothetical_spend_but_zero_verified_sam(n1):
    n1.source("SRC-DEMO", permitted_use=MARKET_USES, data_origin="SYNTHETIC")
    n1.submit_ok(tender(synthetic=True), award(synthetic=True), sam_award(synthetic=True), source_id="SRC-DEMO",
                 synthetic=True)
    rep = n1.report()
    s = rep["sam"]
    assert s["verified_real_sam"] == "0.00" and s["indicative_spend"] == "0.00"
    assert s["hypothetical_addressable_spend"] == "2082000.00"          # (1 250 000 + 400 000) × 1.08 + 300 000
    assert s["synthetic_evidence_count"] == 3 and s["verified_evidence_ids"] == []
    assert all(o["synthetic_only"] and "SYNTHETIC_ONLY" in o["risk_flags"] for o in rep["opportunities"])


def test_synthetic_evidence_cannot_be_attested(n1):
    n1.source("SRC-DEMO", permitted_use=MARKET_USES, data_origin="SYNTHETIC")
    n1.submit_ok(award(synthetic=True), source_id="SRC-DEMO", synthetic=True)
    r = n1.attest("TED-2026-000202")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "SYNTHETIC_NOT_ATTESTABLE"


def test_synthetic_rows_hidden_in_a_real_batch_are_blocked_by_n0(n1_src):
    r = n1_src.submit(award(), award("TED-2026-000303", synthetic=True))
    assert r.json()["n0_decision"]["decision"] == "BLOCK" and r.json()["intake"] is None


@pytest.mark.parametrize("attested", [False, True])
def test_mixed_real_and_synthetic_never_leak(n1, attested):
    n1.source("SRC-TED")
    n1.source("SRC-DEMO", permitted_use=MARKET_USES, data_origin="SYNTHETIC")
    n1.submit_ok(award(), batch_id="REAL")
    n1.submit_ok(sam_award(synthetic=True), source_id="SRC-DEMO", synthetic=True, batch_id="DEMO")
    if attested:
        n1.attest("TED-2026-000202")
    s = n1.report()["sam"]
    assert s["verified_real_sam"] == ("432000.00" if attested else "0.00")
    assert s["hypothetical_addressable_spend"] == "300000.00"
    assert "SAMGOV-ab12cd34" not in s["verified_evidence_ids"]


def test_unattested_verified_budget_is_pending_not_verified(n1_src):
    n1_src.submit_ok(award())
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "0.00" and rep["sam"]["indicative_spend"] == "432000.00"
    assert rep["pending_attestations"] == ["TED-2026-000202"]
    pending = n1_src.get("/v1/n1/attestations/pending", "ana").json()
    assert [p["evidence_id"] for p in pending] == ["TED-2026-000202"]


def test_trade_statistics_are_tam_proxies_not_sam(n1_src):
    trade = tender("COMTRADE-276-0-8504-M-2025", observation_type="TRADE_STATISTIC", source_connector="UN_COMTRADE",
                   buyer_name=None, classification_codes={"hs": ["8504"]},
                   amount={"amount": "5000000.00", "currency": "USD"}, amount_kind="TRADE_VALUE",
                   published_at=iso(days(-200)), deadline_at=None)
    n1_src.submit_ok(trade)
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "0.00"
    assert _feature(rep, "COMTRADE-276-0-8504-M-2025")["spend_type"] == "HYPOTHETICAL"


# ------------------------------------------------------------------------------------------------
# duplicates, corrigenda, staleness
# ------------------------------------------------------------------------------------------------
def test_award_of_a_tender_is_counted_once(n1_src):
    t = tender(amount={"amount": "400000.00", "currency": "EUR"})
    a = award(related_evidence_ids=["TED-2026-000101"])
    n1_src.submit_ok(t, a)
    for eid in ("TED-2026-000101", "TED-2026-000202"):
        n1_src.attest(eid)
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "432000.00"
    assert _feature(rep, "TED-2026-000101")["exclusions"] == ["DUPLICATE_SPEND"]
    assert "TED-2026-000202" in rep["sam"]["verified_evidence_ids"]


def test_same_spend_across_overlapping_accounts_and_connectors_is_deduplicated(n1_src):
    a = award()
    dup = award("CPPP-MIRROR-1", source_connector="MANUAL", source_url="https://example.test/mirror/1",
                source_record_id=None, buyer_name="Stadtwerke Beispielstadt", buyer_identifiers={},
                published_at=iso(days(-55)))
    n1_src.submit_ok(a, dup)
    for eid in ("TED-2026-000202", "CPPP-MIRROR-1"):
        n1_src.attest(eid)
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "432000.00"             # not 864 000
    excluded = {x["evidence_id"]: x["codes"] for x in rep["exclusions"]}
    assert excluded == {"CPPP-MIRROR-1": ["DUPLICATE_SPEND"]} or excluded == {"TED-2026-000202": ["DUPLICATE_SPEND"]}
    assert len({_feature(rep, e)["account_id"] for e in ("TED-2026-000202", "CPPP-MIRROR-1")}) == 1


def test_only_the_latest_corrigendum_counts(n1_src):
    c0 = tender("CPPP-T1-C0", source_connector="INDIA_CPPP", source_record_id="T1", corrigendum_seq=0,
                amount={"amount": "4500000", "currency": "INR"}, buyer_country="IN")
    c1 = tender("CPPP-T1-C1", source_connector="INDIA_CPPP", source_record_id="T1", corrigendum_seq=1,
                amount={"amount": "4800000", "currency": "INR"}, buyer_country="IN")
    n1_src.submit_ok(c0, c1)
    rep = n1_src.report()
    assert _feature(rep, "CPPP-T1-C0")["exclusions"] == ["SUPERSEDED_CORRIGENDUM"]
    assert _feature(rep, "CPPP-T1-C1")["fx"]["amount_base"] == "57600.00"   # 4 800 000 INR × 0.012


def test_stale_evidence_is_excluded(n1_src):
    old_award = award(published_at=iso(days(-800)))
    closed = tender(published_at=iso(days(-40)), deadline_at=iso(days(-1)))
    n1_src.submit_ok(old_award, closed)
    n1_src.attest("TED-2026-000202")
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "0.00"
    assert "STALE_EVIDENCE" in _feature(rep, "TED-2026-000202")["exclusions"]
    assert "deadline" in _feature(rep, "TED-2026-000101")["exclusion_detail"][0]
    assert any(f["code"] == "STALE_EVIDENCE" for f in rep["risk_flags"])


def test_freshness_sla_can_be_tightened_per_request(n1_src):
    n1_src.submit_ok(award())
    n1_src.attest("TED-2026-000202")
    assert n1_src.report()["sam"]["verified_real_sam"] == "432000.00"
    assert n1_src.report(freshness_sla_days={"CONTRACT_AWARD": 30})["sam"]["verified_real_sam"] == "0.00"


# ------------------------------------------------------------------------------------------------
# malformed values, FX, attachments, unsupported claims, rejected attestation, revoked rights
# ------------------------------------------------------------------------------------------------
def test_negative_zero_and_implausible_awards_are_excluded(n1_src):
    n1_src.submit_ok(award("TED-NEG", amount={"amount": "-5000", "currency": "EUR"}),
                     award("TED-ZERO", amount={"amount": "0", "currency": "EUR"}, published_at=iso(days(-200))),
                     award("TED-HUGE", amount={"amount": "5000000000000", "currency": "EUR"}, published_at=iso(days(-300))))
    for eid in ("TED-NEG", "TED-ZERO", "TED-HUGE"):
        n1_src.attest(eid)
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "0.00"
    codes = {x["evidence_id"]: x["codes"] for x in rep["exclusions"]}
    assert codes == {"TED-NEG": ["MALFORMED_AMOUNT"], "TED-ZERO": ["MALFORMED_AMOUNT"], "TED-HUGE": ["IMPLAUSIBLE_AMOUNT"]}


def test_missing_fx_rate_excludes_instead_of_guessing(n1_src):
    n1_src.submit_ok(award(amount={"amount": "100000", "currency": "CHF"}))
    n1_src.attest("TED-2026-000202")
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "0.00"
    assert _feature(rep, "TED-2026-000202")["exclusions"] == ["FX_MISSING"]


def test_missing_mandatory_attachment_disqualifies_a_tender(n1_src):
    t = tender(attachments=[{"name": "NIT.pdf", "required": True, "present": True},
                            {"name": "BOQ.xlsx", "required": True, "present": False}])
    n1_src.submit_ok(t)
    rep = n1_src.report()
    assert _feature(rep, "TED-2026-000101")["exclusions"] == ["MISSING_MANDATORY_ATTACHMENT"]
    assert rep["opportunities"] == []                         # the only evidence is disqualified, nothing to rank


def test_unsupported_manual_claims_are_excluded(n1_src):
    claim = award("MANUAL-1", source_connector="MANUAL", source_url=None, evidence_snippets=[], source_record_id=None)
    n1_src.submit_ok(claim)
    n1_src.attest("MANUAL-1")
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "0.00"
    assert _feature(rep, "MANUAL-1")["exclusions"] == ["UNSUPPORTED_CLAIM"]


def test_rejected_attestation_excludes_evidence(n1_src):
    n1_src.submit_ok(award())
    assert n1_src.attest("TED-2026-000202", status="REJECTED").status_code == 200
    rep = n1_src.report()
    assert _feature(rep, "TED-2026-000202")["exclusions"] == ["ATTESTATION_REJECTED"]


def test_revoked_source_rights_remove_evidence_from_sam(n1_src):
    n1_src.submit_ok(award())
    n1_src.attest("TED-2026-000202")
    assert n1_src.report()["sam"]["verified_real_sam"] == "432000.00"
    r = n1_src.post("/v1/sources/acme/SRC-TED/approve", "rita", json={"decision": "REVOKED",
                                                                      "reason": "licence withdrawn by publisher"})
    assert r.status_code == 200
    rep = n1_src.report()
    assert rep["sam"]["verified_real_sam"] == "0.00"
    assert _feature(rep, "TED-2026-000202")["exclusions"] == ["RIGHTS_REVOKED"]


def test_changed_evidence_invalidates_its_attestation(n1_src):
    n1_src.submit_ok(award(), batch_id="B-1")
    n1_src.attest("TED-2026-000202")
    n1_src.submit_ok(award(amount={"amount": "450000.00", "currency": "EUR"}), batch_id="B-2")
    rep = n1_src.report()
    f = _feature(rep, "TED-2026-000202")
    assert f["evidence_revision"] == 2 and f["attested"] is False and rep["sam"]["verified_real_sam"] == "0.00"
