"""End-to-end local demo on SYNTHETIC buyers (no network, temp database).

    python examples/run_n1_demo.py

It shows the invariant that matters most. Synthetic demo data produces a
``hypothetical_addressable_spend`` figure and a ranked shortlist, but ``verified_real_sam`` stays 0
and the report can never be handed off to N2/N6/N7. Every record below is fictional.
"""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.n1.config import N1Settings  # noqa: E402

DEMO_SECRET = "athena-n1-local-demo-secret-" + "d" * 32
DEMO_USERS = {"demo.analyst": ["analyst"], "demo.approver": ["approver"], "demo.steward": ["source_admin"],
              "demo.rights": ["rights_approver"], "demo.auditor": ["auditor"]}


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def demo_token(user: str) -> str:
    return jwt.encode({"sub": user, "tenant_id": "demo", "roles": DEMO_USERS[user], "iss": "athena-demo",
                       "exp": int(time.time()) + 3600}, DEMO_SECRET, algorithm="HS256")


def synthetic_observations(now: datetime) -> list[dict[str, Any]]:
    d = lambda n: _iso(now + timedelta(days=n))  # noqa: E731
    return [
        {"evidence_id": "DEMO-TENDER-001", "observation_type": "TENDER_NOTICE", "source_connector": "MANUAL",
         "source_url": "https://example.invalid/demo/tender-001", "buyer_name": "Demo Water Utility",
         "buyer_country": "DE", "buyer_type": "PUBLIC", "title": "Power supply units for pumping stations",
         "amount": {"amount": "900000.00", "currency": "EUR"}, "amount_kind": "ESTIMATED_VALUE",
         "published_at": d(-7), "deadline_at": d(30), "quantity": 300, "synthetic": True},
        {"evidence_id": "DEMO-AWARD-002", "observation_type": "CONTRACT_AWARD", "source_connector": "MANUAL",
         "source_url": "https://example.invalid/demo/award-002", "buyer_name": "Demo Rail Operator",
         "buyer_country": "FR", "buyer_type": "PUBLIC", "title": "Battery charger maintenance contract",
         "amount": {"amount": "250000.00", "currency": "EUR"}, "amount_kind": "AWARD_VALUE",
         "published_at": d(-40), "synthetic": True},
        {"evidence_id": "DEMO-INTERVIEW-003", "observation_type": "BUYER_INTERVIEW", "source_connector": "INTERVIEW",
         "source_url": "https://example.invalid/demo/interview-003", "buyer_name": "Demo Robotics Ltd",
         "buyer_country": "GB", "buyer_type": "PRIVATE", "title": "Interview: inverter retrofit",
         "description": "Customer plans an inverter retrofit and states an approved budget.",
         "amount": {"amount": "120000.00", "currency": "GBP"}, "amount_kind": "STATED_SPEND",
         "budget_status": "APPROVED", "relationship_stage": "WARM", "published_at": d(-3), "synthetic": True},
        {"evidence_id": "DEMO-STALE-004", "observation_type": "TENDER_NOTICE", "source_connector": "MANUAL",
         "source_url": "https://example.invalid/demo/tender-004", "buyer_name": "Demo Water Utility",
         "buyer_country": "DE", "title": "Old converter tender (closed)",
         "amount": {"amount": "500000.00", "currency": "EUR"}, "amount_kind": "ESTIMATED_VALUE",
         "published_at": d(-400), "deadline_at": d(-370), "synthetic": True},
    ]


def build_demo(tmpdir: str):
    settings = Settings(database_url=f"sqlite:///{tmpdir}/demo.db", storage_root=f"{tmpdir}/objects", tokens={},
                        jwt_secret=DEMO_SECRET, jwt_issuer="athena-demo")
    client = TestClient(create_app(settings, n1_settings=N1Settings()))
    h = lambda u: {"Authorization": f"Bearer {demo_token(u)}"}  # noqa: E731
    now = datetime.now(timezone.utc).replace(microsecond=0)
    client.post("/v1/sources/register", headers=h("demo.steward"), json={
        "source_id": "SRC-DEMO-SYNTHETIC", "tenant_id": "demo", "publisher": "ATHENA demo generator",
        "license": "LicenseRef-Internal-Demo", "data_origin": "SYNTHETIC",
        "permitted_use": ["MARKET_INTELLIGENCE", "RFQ_ANALYSIS", "COMMERCIAL", "FINANCE"]}).raise_for_status()
    client.post("/v1/sources/demo/SRC-DEMO-SYNTHETIC/approve", headers=h("demo.rights"),
                json={"decision": "APPROVED", "reason": "synthetic demo data for UI walkthroughs"}).raise_for_status()
    sub = client.post("/v1/n1/batches", headers=h("demo.analyst"), json={
        "tenant_id": "demo", "project_id": "PRJ-DEMO", "source_id": "SRC-DEMO-SYNTHETIC", "synthetic": True,
        "batch": {"batch_id": "DEMO-BATCH-1", "connector": "MANUAL", "retrieved_at": _iso(now),
                  "observations": synthetic_observations(now)}})
    sub.raise_for_status()
    fx = [{"currency": "EUR", "rate_to_base": "1.08", "as_of": (now - timedelta(days=1)).date().isoformat(),
           "source": "demo FX table"},
          {"currency": "GBP", "rate_to_base": "1.27", "as_of": (now - timedelta(days=1)).date().isoformat(),
           "source": "demo FX table"}]
    rep = client.post("/v1/n1/reports", headers=h("demo.analyst"), json={
        "tenant_id": "demo", "scope_name": "demo", "serviceable_categories": ["power_electronics"],
        "base_currency": "USD", "fx_rates": fx})
    rep.raise_for_status()
    return client, h, rep.json()


def main() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        client, h, report = build_demo(tmp)
        md = client.get(f"/v1/n1/reports/{report['report_id']}/markdown", headers=h("demo.analyst")).text
        accounts = {o["legal_name"]: o["account_id"] for o in report["opportunities"]}
        opt = client.post("/v1/n1/optimize", headers=h("demo.analyst"), json={
            "tenant_id": "demo", "report_id": report["report_id"], "currency": "USD", "budget_cap": "5000",
            "actions": [
                {"action_id": "A1", "action_type": "DESK_RESEARCH", "currency": "USD", "execution_cost": "400",
                 "expected_loss_avoided": "3000"},
                {"action_id": "A2", "action_type": "SUBMIT_BID", "currency": "USD", "execution_cost": "0",
                 "expected_loss_avoided": "100000"},
                {"action_id": "A3", "action_type": "EMAIL_OUTREACH", "currency": "USD", "execution_cost": "50",
                 "expected_loss_avoided": "800", "target_account_id": next(iter(accounts.values()), None)}]}).json()
        handoff = client.post(f"/v1/n1/reports/{report['report_id']}/handoff", headers=h("demo.approver"), json={
            "targets": ["N2", "N6", "N7"], "derived_source_id": "SRC-DEMO-SYNTHETIC", "project_id": "PRJ-DEMO"})
        sam = report["sam"]
        summary = {"verified_real_sam": sam["verified_real_sam"],
                   "hypothetical_addressable_spend": sam["hypothetical_addressable_spend"],
                   "shortlist": [(o["legal_name"], o["heuristic_priority_score"]) for o in report["opportunities"]],
                   "excluded": [x["evidence_id"] for x in report["exclusions"]],
                   "optimizer_executed": opt["executed"], "blocked_actions": [b["action_id"] for b in opt["blocked"]],
                   "handoff_status": handoff.status_code, "handoff_code": handoff.json()["detail"]["code"]}
        print(md)
        print("Summary:", summary)
        return summary


if __name__ == "__main__":
    main()
