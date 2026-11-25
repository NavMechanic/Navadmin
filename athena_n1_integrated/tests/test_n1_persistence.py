"""Durable persistence (round-trips across app restarts), revision lineage, tenant isolation and
the N0 gate dependency for intake."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from app.n1.config import N1Settings
from app.rules import canonical_json, sha256_hex
from conftest import ISSUER, N1, NOW, SECRET, award, crm, iso, tender


def test_state_survives_an_app_restart_on_the_same_database(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/db.sqlite", storage_root=str(tmp_path / "o"), tokens={},
                 jwt_secret=SECRET, jwt_issuer=ISSUER)
    first = N1(TestClient(a := create_app(s, n1_settings=N1Settings())), a)
    first.source()
    first.submit_ok(award())
    first.attest("TED-2026-000202")
    rep = first.report()
    second = N1(TestClient(b := create_app(s, n1_settings=N1Settings())), b)      # fresh process, same DB
    again = second.get(f"/v1/n1/reports/{rep['report_id']}", "aud").json()
    assert again["report_sha256"] == rep["report_sha256"] and again["sam"]["verified_real_sam"] == "432000.00"
    ev = second.get("/v1/n1/evidence/TED-2026-000202", "aud").json()
    assert ev["attestation_current"] is True and ev["latest"]["n0_artifact"] == "MB-B-1@A"
    assert second.report()["revision"] == 2                                       # lineage continues


def test_evidence_account_and_report_revisions_are_kept(n1_src):
    n1_src.submit_ok(award(), batch_id="B-1")
    n1_src.submit_ok(award(amount={"amount": "410000.00", "currency": "EUR"}), batch_id="B-2")
    ev = n1_src.get("/v1/n1/evidence/TED-2026-000202", "aud").json()
    assert [r["revision"] for r in ev["revisions"]] == [1, 2]
    assert [r["n0_artifact"] for r in ev["revisions"]] == ["MB-B-1@A", "MB-B-2@A"]
    assert ev["latest"]["observation"]["amount"]["amount"] == "410000.00"

    r = n1_src.post("/v1/n1/buyers", "ana", json={"tenant_id": "acme", "project_id": "PRJ-MKT-2027",
                                                  "source_id": "SRC-TED",
                                                  "records": {"batch_id": "CRM-1", "connector": "CRM",
                                                              "retrieved_at": iso(NOW), "records": [crm()]}})
    acc_id = r.json()["intake"]["accounts"][0]
    acc = n1_src.get(f"/v1/n1/buyers/{acc_id}", "aud").json()
    assert len(acc["revisions"]) >= 2 and acc["latest"]["relationship_stage"] == "WARM"
    assert acc["latest"]["named_buyer_validated"] is True and acc["revisions"][0]["named_buyer_validated"] is False
    assert "Stadtwerke Beispielstadt" in acc["latest"]["aliases"]                 # CRM spelling merged by VAT id

    r1, r2 = n1_src.report(), n1_src.report()
    assert r2["supersedes_report_id"] == r1["report_id"]
    listed = {x["report_id"]: x["superseded"] for x in n1_src.get("/v1/n1/reports", "aud").json()}
    assert listed == {r1["report_id"]: True, r2["report_id"]: False}


def test_n1_tables_reject_updates_in_the_database(n1_src):
    n1_src.submit_ok(award())
    n1_src.report()
    for stmt in ("UPDATE n1_observations SET synthetic = 1", "DELETE FROM n1_buyer_accounts",
                 "UPDATE n1_reports SET report_sha256 = 'x'", "DELETE FROM n1_batches"):
        try:
            with n1_src.app.state.db.engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:                                                   # noqa: BLE001
            assert "append-only" in str(exc)
        else:
            raise AssertionError(f"{stmt} was allowed")


def test_tenants_are_isolated(n1):
    n1.source()
    n1.source("SRC-G", tenant="globex")
    n1.submit_ok(award())
    rep = n1.report()
    assert n1.get(f"/v1/n1/reports/{rep['report_id']}", "gina").status_code == 404
    assert n1.get("/v1/n1/evidence/TED-2026-000202", "gina").status_code == 404
    assert n1.get("/v1/n1/reports", "gina").json() == []
    assert n1.post("/v1/n1/intake", "gina", json={"artifact_id": "MB-B-1", "revision": "A"}).status_code == 404
    assert n1.post("/v1/n1/reports", "gina", json={"tenant_id": "acme", "serviceable_categories": ["x"]}).status_code == 403
    assert n1.post("/v1/n1/attestations", "gapr", json={"evidence_id": "TED-2026-000202", "status": "ATTESTED",
                                                        "method": "DOCUMENT_REVIEW", "notes": "cross tenant"}
                   ).status_code == 404
    g = n1.submit(award(), who="gina", tenant="globex", source_id="SRC-G").json()
    assert g["n0_decision"]["decision"] == "PASS"
    grep = n1.post("/v1/n1/reports", "gina", json={"tenant_id": "globex", "serviceable_categories": ["power_electronics"],
                                                   "fx_rates": rep["fx_table"]}).json()
    assert grep["sam"]["verified_real_sam"] == "0.00" and [c["evidence_id"] for c in grep["citations"]] == ["TED-2026-000202"]
    assert grep["opportunities"][0]["account_id"] != rep["opportunities"][0]["account_id"]   # ids are tenant-salted


# ------------------------------------------------------------------------------------------------
# N0 gate dependency
# ------------------------------------------------------------------------------------------------
def _market_artifact(aid: str, payload: dict, source_id: str = "SRC-TED", **kw) -> dict:
    return {"tenant_id": "acme", "project_id": "PRJ-1", "artifact_id": aid, "artifact_type": "MARKET_BATCH",
            "source_id": source_id, "processing_purpose": "MARKET_INTELLIGENCE", "release_stage": "INTAKE",
            "revision": "A", "synthetic": False, "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload,
            **kw}


def test_intake_requires_an_n0_pass(n1):
    n1.source("SRC-PENDING", approve=False)
    r = n1.submit(award(), source_id="SRC-PENDING")
    assert r.json()["n0_decision"]["decision"] == "BLOCK" and r.json()["intake"] is None
    assert n1.post("/v1/n1/intake", "ana", json={"artifact_id": "MB-B-1", "revision": "A"}).status_code == 404

    n1.source()
    payload = {"batch_id": "B-R", "connector": "TED_EU", "retrieved_at": iso(NOW),
               "observations": [award(buyer_name="Contact buyer@example-water.test for details")]}
    res = n1.post("/v1/gateway/evaluate", "ana", json={"artifact": _market_artifact("MB-REVIEW", payload)}).json()
    assert res["decision"]["decision"] == "REVIEW"                                     # PII in the payload
    r = n1.post("/v1/n1/intake", "ana", json={"artifact_id": "MB-REVIEW", "revision": "A"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "N0_NOT_PASS"


def _evaluate(n1, artifact) -> dict:
    r = n1.post("/v1/gateway/evaluate", "ana", json={"artifact": artifact})
    assert r.status_code == 200, r.text
    return r.json()


def _payload(batch_id: str) -> dict:
    return {"batch_id": batch_id, "connector": "TED_EU", "retrieved_at": iso(NOW), "observations": [award()]}


def test_intake_refuses_superseded_revisions_and_is_idempotent(n1_src):
    for rev in ("A", "B"):
        assert _evaluate(n1_src, _market_artifact("MB-X", _payload("B-X"), revision=rev))["decision"]["decision"] == "PASS"
    r = n1_src.post("/v1/n1/intake", "ana", json={"artifact_id": "MB-X", "revision": "A"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SUPERSEDED_REVISION"
    ok = n1_src.post("/v1/n1/intake", "ana", json={"artifact_id": "MB-X", "revision": "B"})
    assert ok.status_code == 200 and ok.json()["observations_ingested"] == 1 and ok.json()["already_ingested"] is False
    again = n1_src.post("/v1/n1/intake", "ana", json={"artifact_id": "MB-X", "revision": "B"}).json()
    assert again["already_ingested"] is True


def test_intake_detects_a_tampered_stored_payload(n1_src):
    payload = _payload("B-Y")
    _evaluate(n1_src, _market_artifact("MB-Y", payload))
    store = n1_src.app.state.store
    path = store._path("quarantine", store.key_for("acme", sha256_hex(canonical_json(payload))))
    path.chmod(0o644)
    path.write_bytes(b'{"tampered": true}')
    r = n1_src.post("/v1/n1/intake", "ana", json={"artifact_id": "MB-Y", "revision": "A"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SHA256_MISMATCH"


def test_intake_reads_market_artifacts_only(n1_src):
    rfq = {"rfq_id": "RFQ-9", "customer_name": "Example", "product_category": "power_electronics", "quantity": 1,
           "requirements": [{"req_id": "R-1", "parameter": "output_voltage_v", "value": 24, "unit": "V",
                             "value_origin": "CUSTOMER"}]}
    art = {**_market_artifact("RFQ-9", rfq), "artifact_type": "RFQ", "processing_purpose": "RFQ_ANALYSIS",
           "release_stage": "CONCEPT"}
    assert _evaluate(n1_src, art)["decision"]["decision"] == "PASS"
    r = n1_src.post("/v1/n1/intake", "ana", json={"artifact_id": "RFQ-9", "revision": "A"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NOT_A_MARKET_ARTIFACT"


def test_intake_rechecks_source_rights(n1_src):
    _evaluate(n1_src, _market_artifact("MB-Z", _payload("B-Z")))
    n1_src.post("/v1/sources/acme/SRC-TED/approve", "rita", json={"decision": "REVOKED", "reason": "licence withdrawn"})
    r = n1_src.post("/v1/n1/intake", "ana", json={"artifact_id": "MB-Z", "revision": "A"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "N0_RIGHTS_DENIED"
