"""Baseline — N0 gateway behaviour inside the integrated app (JWT auth, market artifacts)."""
from __future__ import annotations

from sqlalchemy import text

from app.rules import canonical_json, sha256_hex
from conftest import NOW, iso, tender


def _rfq(aid: str = "RFQ-1", power: int = 120, **kw) -> dict:
    payload = {"rfq_id": aid, "customer_name": "Example Industrial Controls", "product_category": "power_supply",
               "quantity": 100, "compliance_standards": ["IEC 62368-1"], "requirements": [
                   {"req_id": "R-1", "parameter": "input_voltage_v", "value": 48, "unit": "V", "value_origin": "CUSTOMER"},
                   {"req_id": "R-2", "parameter": "output_voltage_v", "value": 24, "unit": "V", "value_origin": "CUSTOMER"},
                   {"req_id": "R-3", "parameter": "output_current_a", "value": 5, "unit": "A", "value_origin": "CUSTOMER"},
                   {"req_id": "R-4", "parameter": "output_power_w", "value": power, "unit": "W", "value_origin": "CUSTOMER"},
                   {"req_id": "R-5", "parameter": "operating_temp_min_c", "value": -40, "unit": "degC",
                    "value_origin": "CUSTOMER"},
                   {"req_id": "R-6", "parameter": "operating_temp_max_c", "value": 85, "unit": "degC",
                    "value_origin": "CUSTOMER"}]}
    return {"tenant_id": "acme", "project_id": "PRJ-1", "artifact_id": aid, "artifact_type": "RFQ",
            "source_id": kw.pop("source_id", "SRC-TED"), "processing_purpose": "RFQ_ANALYSIS",
            "release_stage": "CONCEPT", "revision": "A", "synthetic": False,
            "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload, **kw}


def _evaluate(n1, artifact, who="ana"):
    r = n1.post("/v1/gateway/evaluate", who, json={"artifact": artifact})
    assert r.status_code == 200, r.text
    return r.json()


def test_health_reports_n0_components_with_n1_mounted(n1):
    body = n1.c.get("/health").json()
    assert body["status"] == "ok" and body["components"]["database"]["ok"]
    assert body["components"]["database"]["missing_tables"] == []              # N1 tables included
    assert n1.get("/v1/n1/status", "ana").json()["engine_version"].startswith("n1-engine/")


def test_source_rights_need_a_second_person(n1):
    n1.source("SRC-X", approve=False)
    r = n1.post("/v1/sources/acme/SRC-X/approve", "sam", json={"decision": "APPROVED", "reason": "self approval"})
    assert r.status_code == 403
    r = n1.post("/v1/sources/acme/SRC-X/approve", "rita", json={"decision": "APPROVED", "reason": "licence checked"})
    assert r.status_code == 200 and r.json()["approval_event"]["scope"] == "SOURCE_RIGHTS"


def test_upload_is_quarantined_and_sha256_addressed(n1_src):
    data = b"Contracting authority: Example Board\nEstimated value: EUR 100,000\n"
    r = n1_src.post("/v1/artifacts/upload", "ana", data={"tenant_id": "acme", "source_id": "SRC-TED"},
                    files={"file": ("notice.txt", data, "text/plain")})
    assert r.status_code == 201, r.text
    assert r.json()["file_ref"]["uri"] == f"n0://quarantine/acme/{sha256_hex(data)}"


def test_valid_rfq_passes_with_seven_deliverables(n1_src):
    res = _evaluate(n1_src, _rfq())
    assert res["decision"]["decision"] == "PASS"
    assert set(res) == {"canonical_artifact", "provenance", "quality_report", "rights_decision", "decision",
                        "release_manifest", "audit_event"}


def test_power_inconsistent_rfq_is_rework(n1_src):
    res = _evaluate(n1_src, _rfq("RFQ-2", power=150))
    assert res["decision"]["decision"] == "REWORK" and res["decision"]["allowed_destinations"] == []


def test_unapproved_source_blocks(n1):
    n1.source("SRC-PENDING", approve=False)
    res = _evaluate(n1, _rfq("RFQ-3", source_id="SRC-PENDING"))
    assert res["decision"]["decision"] == "BLOCK"
    assert "SOURCE_NOT_APPROVED" in {v["code"] for v in res["decision"]["violated_rules"]}


def test_cross_tenant_submission_blocks_and_is_not_stored(n1_src):
    a = _rfq("RFQ-X")
    a["tenant_id"] = "globex"
    res = _evaluate(n1_src, a)
    assert res["decision"]["decision"] == "BLOCK" and res["canonical_artifact"] is None
    assert n1_src.get("/v1/artifacts/RFQ-X", "gina").status_code == 404


def test_market_batch_routes_only_to_n1_and_synthetic_rows_taint_it(n1_src):
    payload = {"batch_id": "B-N0", "connector": "TED_EU", "retrieved_at": iso(NOW), "observations": [tender()]}
    art = {"tenant_id": "acme", "project_id": "PRJ-1", "artifact_id": "MB-N0", "artifact_type": "MARKET_BATCH",
           "source_id": "SRC-TED", "processing_purpose": "MARKET_INTELLIGENCE", "release_stage": "INTAKE",
           "revision": "A", "synthetic": False, "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload}
    ok = _evaluate(n1_src, art)
    assert ok["decision"]["decision"] == "PASS" and ok["decision"]["allowed_destinations"] == ["N1"]
    payload["observations"] = [tender(synthetic=True)]
    art.update(artifact_id="MB-N0-2", source_sha256=sha256_hex(canonical_json(payload)), payload=payload)
    bad = _evaluate(n1_src, art)
    assert bad["decision"]["decision"] == "BLOCK"
    assert "SYNTHETIC_AS_REAL" in {v["code"] for v in bad["decision"]["violated_rules"]}


def test_sam_evidence_totals_are_recomputed_by_n0(n1):
    n1.source("SRC-N1-DERIVED", permitted_use=("FINANCE",))
    item = {"evidence_id": "E1", "account_id": "A1", "spend_type": "VERIFIED_BUDGET", "synthetic": False,
            "attestation_id": "ATT-1", "original": {"amount": "100.00", "currency": "EUR"}, "fx_rate": "1.08",
            "fx_as_of": NOW.date().isoformat(), "amount_base": "108.00"}
    payload = {"report_id": "RPT-1", "report_sha256": "b" * 64, "as_of": NOW.date().isoformat(), "base_currency": "USD",
               "verified_real_sam": "999.00", "indicative_spend": "0", "hypothetical_addressable_spend": "0",
               "verified_items": [item]}
    art = {"tenant_id": "acme", "project_id": "PRJ-1", "artifact_id": "SAM-1", "artifact_type": "SAM_EVIDENCE",
           "source_id": "SRC-N1-DERIVED", "processing_purpose": "FINANCE", "release_stage": "CONCEPT",
           "revision": "A", "synthetic": False, "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload}
    res = _evaluate(n1, art)
    assert res["decision"]["decision"] == "REWORK"
    assert "SAM_TOTAL_MISMATCH" in {v["code"] for v in res["decision"]["violated_rules"]}


def test_audit_chain_verifies_and_tables_are_append_only(n1_src):
    _evaluate(n1_src, _rfq())
    n1_src.submit_ok(tender())                                                # rows to protect (row triggers)
    n1_src.report()
    assert n1_src.get("/v1/audit/verify", "aud").json()["audit"]["ok"]
    for stmt in ("UPDATE audit_events SET actor_id='x'", "DELETE FROM n1_reports", "UPDATE n1_observations SET revision=9"):
        try:
            with n1_src.app.state.db.engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:                                               # noqa: BLE001
            assert "append-only" in str(exc)
        else:
            raise AssertionError(f"{stmt} was allowed")
