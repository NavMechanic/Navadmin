"""Baseline — N1 contracts, deterministic engine (N1.2–N1.5), connectors, explainer and the demo."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal

import httpx
from jsonschema import Draft202012Validator

from app.models import MarketObservation
from app.n1 import engine
from app.n1.models import (N1_SCHEMAS, ActionOptimizationRequest, BuyerAccount, FxRate, ReportRequest)
from conftest import NOW, ROOT, award, tender


def _obs(**kw) -> MarketObservation:
    return MarketObservation.model_validate(tender(**kw))


def _acc(**kw) -> BuyerAccount:
    base = dict(account_id="ACC-1", tenant_id="acme", revision=1, legal_name="Stadtwerke Beispielstadt GmbH",
                normalized_name=engine.normalize_name("Stadtwerke Beispielstadt GmbH"), country="DE",
                buyer_type="PUBLIC", identifiers={"vat": "DE999999999"}, updated_at=NOW, synthetic_only=False)
    base.update(kw)
    return BuyerAccount(**base)


def test_n1_contracts_publish_valid_2020_12_schemas():
    assert set(N1_SCHEMAS) >= {"n1_market_batch", "n1_market_observation", "n1_buyer_account",
                               "n1_opportunity_assessment", "n1_market_report", "n1_action_optimization_request",
                               "n1_action_optimization_result", "n1_evidence_attestation", "n1_feedback_case",
                               "n1_connector_import", "n1_connector_preview"}
    for model in N1_SCHEMAS.values():
        Draft202012Validator.check_schema(model.model_json_schema())


def test_fx_conversion_is_decimal_with_an_explicit_rate_date():
    rates = [FxRate(currency="EUR", rate_to_base=Decimal("1.0815"), as_of=date(2026, 11, 20), source="ECB"),
             FxRate(currency="EUR", rate_to_base=Decimal("1.0900"), as_of=date(2026, 12, 20), source="ECB")]
    fx = engine.convert(_obs(amount={"amount": "1000.005", "currency": "EUR"}), "USD", rates, date(2026, 11, 25))
    assert fx.rate == Decimal("1.0815") and fx.fx_as_of == date(2026, 11, 20)   # never a rate from the future
    assert fx.amount_base == Decimal("1081.51") and not fx.fx_stale
    assert engine.convert(_obs(amount={"amount": "1", "currency": "JPY"}), "USD", rates, date(2026, 11, 25)) is None
    old = engine.convert(_obs(), "USD", rates[:1], date(2027, 1, 30))
    assert old.fx_stale


def test_spend_type_classification():
    cases = [(award(), "VERIFIED_BUDGET"), (tender(), "INDICATIVE"),
             (tender(observation_type="BUDGET_DOCUMENT", budget_status="APPROVED"), "VERIFIED_BUDGET"),
             (tender(observation_type="BUDGET_DOCUMENT", budget_status="PLANNED"), "INDICATIVE"),
             (tender(observation_type="TRADE_STATISTIC", source_connector="UN_COMTRADE"), "HYPOTHETICAL"),
             (award(synthetic=True), "HYPOTHETICAL"), (tender(amount=None), "HYPOTHETICAL")]
    for raw, expected in cases:
        assert engine.classify_spend(MarketObservation.model_validate(raw))[0] == expected, raw["evidence_id"]


def test_entity_resolution_is_deterministic_and_country_bounded():
    accs = [_acc()]
    assert engine.match_account("Other name", {"VAT": "de999999999"}, "DE", accs)[1] == "IDENTIFIER"
    assert engine.match_account("Stadtwerke Beispielstadt", {}, "DE", accs)[1] == "EXACT_NAME"
    assert engine.match_account("Stadtwerke Beispielstad GmbH", {}, "DE", accs)[1] == "FUZZY_NAME"
    assert engine.match_account("Stadtwerke Beispielstadt", {}, "AT", accs) == (None, "NEW")
    assert engine.normalize_name("The U.S. Example Agency, Inc.") == "us example agency"


def test_service_category_tagging():
    assert engine.tag_category(_obs(service_category="pcb_assembly")) == ("pcb_assembly", "GIVEN")
    assert engine.tag_category(_obs()) == ("power_electronics", "CODE")
    assert engine.tag_category(_obs(classification_codes={}, title="SCADA automation upgrade")) == \
        ("industrial_automation", "KEYWORD")
    assert engine.tag_category(_obs(classification_codes={}, title="Office furniture")) == (None, "NONE")


def test_opportunity_score_is_a_transparent_heuristic_not_a_probability():
    req = ReportRequest(tenant_id="acme", serviceable_categories=["power_electronics"],
                        fx_rates=[FxRate(currency="EUR", rate_to_base=Decimal("1.08"), as_of=NOW.date(), source="ECB")])
    inp = [engine.EvidenceInput(obs=_obs(), revision=1, sha256="a" * 64, account_id="ACC-1", n0_artifact="MB-1@A")]
    report = engine.build_report(report_id="RPT-1", tenant_id="acme", request=req, as_of=NOW, generated_at=NOW,
                                 generated_by="ana", scope_key="SCP-1", revision=1, supersedes=None, inputs=inp,
                                 accounts={"ACC-1": _acc()}, labeled_outcomes=0)
    o = report.opportunities[0]
    total = sum(c.points for c in o.components) + max(-30, sum(p.points for p in o.penalties))
    assert o.heuristic_priority_score == round(max(0, min(100, total)), 1)
    assert o.score_kind == "HEURISTIC_PRIORITIZATION" and o.calibrated is False and o.win_probability is None
    assert {c.name for c in o.components} == {"buyer_access", "requirements_fit", "procurement_evidence_strength",
                                              "timing"}
    assert report.ml_readiness.ready is False and "not a calibrated probability" in report.disclaimers[1].lower() \
        or "NOT a calibrated probability" in report.disclaimers[1]


def test_optimizer_gates_policy_first_then_ranks_by_net_value():
    accs = {"ACC-1": _acc(), "ACC-2": _acc(account_id="ACC-2", do_not_contact=True),
            "ACC-3": _acc(account_id="ACC-3", synthetic_only=True)}
    req = ActionOptimizationRequest(tenant_id="acme", currency="USD", budget_cap=Decimal("1000"), actions=[
        {"action_id": "BID", "action_type": "SUBMIT_BID", "currency": "USD", "execution_cost": "0",
         "expected_loss_avoided": "1000000"},
        {"action_id": "SCRAPE", "action_type": "PORTAL_SCRAPE", "currency": "USD", "execution_cost": "0",
         "expected_loss_avoided": "50"},
        {"action_id": "MAIL-QUIET", "action_type": "EMAIL_OUTREACH", "target_account_id": "ACC-1", "currency": "USD",
         "execution_cost": "10", "expected_loss_avoided": "500"},
        {"action_id": "MAIL-DNC", "action_type": "EMAIL_OUTREACH", "target_account_id": "ACC-2", "currency": "USD",
         "execution_cost": "10", "expected_loss_avoided": "500"},
        {"action_id": "MAIL-SYN", "action_type": "REQUEST_BUYER_INTERVIEW", "target_account_id": "ACC-3",
         "currency": "USD", "execution_cost": "10", "expected_loss_avoided": "500"},
        {"action_id": "EUR", "action_type": "DESK_RESEARCH", "currency": "EUR", "execution_cost": "1",
         "expected_loss_avoided": "9"},
        {"action_id": "CLARIFY", "action_type": "SUBMIT_CLARIFICATION_QUESTION", "target_account_id": "ACC-1",
         "currency": "USD", "execution_cost": "100", "expected_loss_avoided": "2000", "delay_cost_per_day": "50",
         "expected_delay_days": 4},
        {"action_id": "RESEARCH", "action_type": "DESK_RESEARCH", "currency": "USD", "execution_cost": "600",
         "expected_loss_avoided": "3000"},
        {"action_id": "DATA", "action_type": "PURCHASE_MARKET_DATA", "currency": "USD", "execution_cost": "900",
         "expected_loss_avoided": "1200"},
        {"action_id": "LOSER", "action_type": "DESK_RESEARCH", "currency": "USD", "execution_cost": "500",
         "expected_loss_avoided": "100"}])
    ranked, blocked, total = engine.optimize_actions(req, accs, quiet={"ACC-1"})
    codes = {b.action_id: b.policy_codes for b in blocked}
    assert codes == {"BID": ["BID_SUBMISSION_IS_N6"], "SCRAPE": ["SCRAPING_PROHIBITED"],
                     "MAIL-QUIET": ["PROCUREMENT_QUIET_PERIOD"], "MAIL-DNC": ["DO_NOT_CONTACT"],
                     "MAIL-SYN": ["SYNTHETIC_TARGET"], "EUR": ["CURRENCY_MISMATCH"]}
    assert [r.action_id for r in ranked] == ["RESEARCH", "CLARIFY", "DATA", "LOSER"]
    assert ranked[1].net_value == Decimal("1700.00")                  # 2000 − 100 − 50 × 4
    assert [r.recommended for r in ranked] == [True, True, False, False]   # DATA exceeds the 1000 budget cap
    assert total == Decimal("700.00") and all(r.status == "RECOMMENDED_NOT_EXECUTED" for r in ranked)


def test_optimizer_endpoint_never_calls_out(n1_src, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("N1.5 must not perform network I/O")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    r = n1_src.post("/v1/n1/optimize", "ana", json={"tenant_id": "acme", "currency": "USD", "actions": [
        {"action_id": "A1", "action_type": "AUTO_OUTREACH_CAMPAIGN", "currency": "USD", "execution_cost": "1",
         "expected_loss_avoided": "999"}]})
    assert r.status_code == 200 and r.json()["executed"] is False
    assert r.json()["blocked"][0]["policy_codes"] == ["AUTONOMOUS_OUTREACH_PROHIBITED"]


def test_sam_gov_offline_sample_normalizes(n1_src):
    raw = json.loads((ROOT / "examples/samples/sam_gov_opportunities.json").read_text())
    r = n1_src.post("/v1/n1/connectors/sam_gov/preview", "ana", json={"tenant_id": "acme", "raw_payload": raw})
    assert r.status_code == 200, r.text
    obs = {o["evidence_id"]: o for o in r.json()["observations"]}
    a, t = obs["SAMGOV-f1e2d3c4b5a60001"], obs["SAMGOV-f1e2d3c4b5a60002"]
    assert a["observation_type"] == "CONTRACT_AWARD" and a["amount"] == {"amount": "300000.00", "currency": "USD"}
    assert a["buyer_name"] == "DEPARTMENT OF EXAMPLE AFFAIRS" and a["classification_codes"]["set_aside"] == ["SBA"]
    assert t["observation_type"] == "TENDER_NOTICE" and t["deadline_at"].startswith("2026-12-20T22:00:00")
    assert all(o["raw_sha256"] == r.json()["raw_sha256"] for o in obs.values())


def test_explainer_is_disabled_by_default(n1_src):
    n1_src.submit_ok(tender())
    rep = n1_src.report()
    r = n1_src.post(f"/v1/n1/reports/{rep['report_id']}/explain", "ana")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "EXPLAINER_DISABLED"


def test_local_demo_keeps_verified_sam_at_zero():
    out = subprocess.run([sys.executable, str(ROOT / "examples/run_local_demo.py")], capture_output=True, text=True,
                         timeout=180, cwd=ROOT)
    assert out.returncode == 0, out.stderr[-2000:]
    summary = out.stdout.splitlines()[-1]
    assert "'verified_real_sam': '0.00'" in summary and "'hypothetical_addressable_spend': '1394400.00'" in summary
    assert "'handoff_code': 'SYNTHETIC_ONLY_REPORT'" in summary and "'optimizer_executed': False" in summary


def test_markdown_report_cites_evidence_and_sources(n1_src):
    n1_src.submit_ok(tender(), award())
    n1_src.attest("TED-2026-000202")
    rep = n1_src.report()
    md = n1_src.get(f"/v1/n1/reports/{rep['report_id']}/markdown", "ana").text
    assert "[TED-2026-000202](https://ted.europa.eu/en/notice/-/detail/2026-000202)" in md
    assert "**Verified real SAM** | 432,000.00" in md and "NOT a calibrated probability" in md
    assert "MB-B-1@A" in md                                              # every citation names its N0 artifact
    assert (NOW - timedelta(days=60)).date().isoformat() in md
