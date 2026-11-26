"""Gateway: baseline behaviour, the critical negative cases (100 % blocked from release),
value_origin separation, revision control, uploads, authN/Z and audit integrity."""
from __future__ import annotations

import copy

import pytest
from sqlalchemy import text

from app.rules import canonical_json, decide, quality_score, sha256_hex
from conftest import RFQ_SOURCE, codes, drop_req, load_example, reseal, rfq, set_req

DELIVERABLES = {"canonical_artifact", "provenance", "quality_report", "rights_decision", "decision",
                "release_manifest", "audit_event"}


# ------------------------------------------------------------------------------------------------
# baseline
# ------------------------------------------------------------------------------------------------
def test_health_reports_components(n0):
    r = n0.c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    comps = body["components"]
    assert comps["database"]["ok"] and comps["object_storage"]["ok"]
    assert comps["policy_engine"]["validator_version"].startswith("n0-validator/")
    assert {"document", "pii"} <= set(comps["parsers"])
    assert comps["llm"]["prompt_revision"]


def test_register_then_approve_source_with_four_eyes(n0):
    rec = n0.source(approve=False)
    assert rec["approval_status"] == "PENDING" and rec["registered_by"] == "sam"
    r = n0.post(f"/v1/sources/acme/{RFQ_SOURCE}/approve", "sam", json={"decision": "APPROVED", "reason": "self ok?"})
    assert r.status_code == 403                                     # source_admin lacks rights_approver
    r = n0.post(f"/v1/sources/acme/{RFQ_SOURCE}/approve", "rita", json={"decision": "APPROVED",
                                                                       "reason": "MNDA countersigned"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["approval_status"] == "APPROVED" and body["source"]["approved_by"] == "rita"
    ev = body["approval_event"]
    assert ev["scope"] == "SOURCE_RIGHTS" and ev["artifact_id"] == f"source:{RFQ_SOURCE}" and len(ev["event_hash"]) == 64
    r = n0.post(f"/v1/sources/acme/{RFQ_SOURCE}/approve", "rita", json={"decision": "APPROVED", "reason": "again!"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INVALID_TRANSITION"


def test_upload_is_quarantined_and_sha256_addressed(n0_src):
    data = b"Output voltage: 24 V\nOutput current: 5 A\n"
    r = n0_src.upload(data)
    assert r.status_code == 201, r.text
    blob = r.json()
    assert blob["sha256"] == sha256_hex(data) and blob["status"] == "SCAN_PASSED"
    assert blob["file_ref"]["uri"] == f"n0://quarantine/acme/{sha256_hex(data)}"
    again = n0_src.upload(data)
    assert again.status_code == 200 and again.json()["blob_id"] == blob["blob_id"]      # idempotent, write-once


def test_valid_rfq_passes_with_all_seven_deliverables(n0_src):
    res = n0_src.evaluate(load_example("valid_rfq.json"))
    assert set(res) == DELIVERABLES
    d = res["decision"]
    assert d["decision"] == "PASS" and d["violated_rules"] == [] and d["missing_fields"] == []
    assert "N2" in d["allowed_destinations"] and "N0" not in d["allowed_destinations"]
    assert "not engineering qualification" in d["qualification_disclaimer"]
    assert d["validator_version"].startswith("n0-validator/")
    assert res["rights_decision"]["status"] == "GRANTED"
    assert res["release_manifest"]["status"] == "RELEASABLE" and len(res["release_manifest"]["manifest_sha256"]) == 64
    assert res["audit_event"]["event_type"] == "GATEWAY_EVALUATION"
    assert set(res["audit_event"]["stage_timings_ms"]) >= {"N0.1", "N0.2", "N0.3", "N0.4", "N0.5"}
    assert res["provenance"]["payload_sha256"] == res["canonical_artifact"]["source_sha256"]


def test_invalid_rfq_is_returned_for_rework(n0_src):
    res = n0_src.evaluate(load_example("invalid_rfq.json"))
    assert res["decision"]["decision"] == "REWORK"
    assert {"POWER_BALANCE_MISMATCH", "MISSING_CRITICAL_FIELD", "DUPLICATE_REQUIREMENT_ID"} <= codes(res)
    assert res["decision"]["allowed_destinations"] == []
    assert any("operating_temp_min_c+operating_temp_max_c" in m for m in res["decision"]["missing_fields"])


# ------------------------------------------------------------------------------------------------
# critical negative cases — each must end with no destination, no release approval, no handoff
# ------------------------------------------------------------------------------------------------
def _unapproved_source(n0):
    n0.source("SRC-PENDING", approve=False)
    return rfq("RFQ-NEG-1", source_id="SRC-PENDING"), "BLOCK", "SOURCE_NOT_APPROVED"


def _cross_tenant(n0):
    n0.source("SRC-GLOBEX", tenant="globex")
    return rfq("RFQ-NEG-2", tenant_id="globex", source_id="SRC-GLOBEX"), "BLOCK", "CROSS_TENANT_REFERENCE"


def _sha_mismatch(n0):
    a = rfq("RFQ-NEG-3")
    a["source_sha256"] = "0" * 64
    return a, "BLOCK", "SHA256_MISMATCH"


def _superseded(n0):
    assert n0.evaluate(rfq("RFQ-NEG-4", "B"))["decision"]["decision"] == "PASS"
    return rfq("RFQ-NEG-4", "A"), "BLOCK", "SUPERSEDED_REVISION"


def _power_inconsistent(n0):
    return set_req(rfq("RFQ-NEG-5"), "output_power_w", value=150), "REWORK", "POWER_BALANCE_MISMATCH"


def _missing_critical_spec(n0):
    return drop_req(rfq("RFQ-NEG-6"), "output_voltage_v"), "REWORK", "MISSING_CRITICAL_FIELD"


def _synthetic_as_real(n0):
    n0.source("SRC-SYNTH", data_origin="SYNTHETIC")
    return rfq("RFQ-NEG-7", source_id="SRC-SYNTH", synthetic=False), "BLOCK", "SYNTHETIC_AS_REAL"


def _synthetic_marker_as_real(n0):
    a = rfq("RFQ-NEG-8")
    a["payload"]["requirements"][0]["operating_conditions"] = {"_synthetic": True}
    return reseal(a), "BLOCK", "SYNTHETIC_AS_REAL"


NEGATIVE_CASES = [_unapproved_source, _cross_tenant, _sha_mismatch, _superseded, _power_inconsistent,
                  _missing_critical_spec, _synthetic_as_real, _synthetic_marker_as_real]


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=lambda f: f.__name__.strip("_"))
def test_critical_negative_cases_never_release(n0_src, case):
    artifact, expected, code = case(n0_src)
    res = n0_src.evaluate(artifact)
    d = res["decision"]
    assert d["decision"] == expected and code in codes(res)
    assert d["allowed_destinations"] == [] and res["release_manifest"]["status"] == "NOT_RELEASED"
    approval = n0_src.approve_release(artifact["artifact_id"], artifact["revision"])
    assert approval.status_code in (403, 404, 409)
    handoff = n0_src.release(artifact["artifact_id"], artifact["revision"])
    assert handoff.status_code in (404, 409)


def test_unapproved_vendor_transmission_is_blocked(n0_src):
    assert n0_src.evaluate(rfq("RFQ-VND-1"))["decision"]["decision"] == "PASS"
    wp = {"work_package_id": "WP-001", "tenant_id": "acme", "project_id": "PRJ-PSU-24V-120W", "vendor_id": "VND-SIM",
          "connector_type": "REST_GRAPHQL", "approved_input_ids": ["RFQ-VND-1@A"],
          "budget_cap": {"amount": "500", "currency": "USD"},
          "expected_outputs": [{"name": "loss_model", "format": "application/json"}]}
    r = n0_src.post("/v1/vendor/work-packages", "rm", json=wp)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["authorized"] is False and d["authorized_inputs"] == []
    assert {v["code"] for v in d["violated_rules"]} == {"VENDOR_TRANSMISSION_UNAPPROVED"}
    assert all(v["severity"] == "BLOCK" for v in d["violated_rules"])
    assert "VENDOR_TRANSMISSION" in d["violated_rules"][0]["message"]


def test_cross_tenant_submission_is_not_persisted(n0_src):
    n0_src.source("SRC-GLOBEX", tenant="globex")
    res = n0_src.evaluate(rfq("RFQ-X", tenant_id="globex", source_id="SRC-GLOBEX"))
    assert res["decision"]["decision"] == "BLOCK" and res["canonical_artifact"] is None
    assert n0_src.c.get("/v1/artifacts/RFQ-X", headers=n0_src.h("gp")).status_code == 404
    assert n0_src.c.get("/v1/artifacts/RFQ-X", headers=n0_src.h("aud")).status_code == 404


def test_cross_tenant_file_reference_is_blocked(n0_src):
    n0_src.source("SRC-GLOBEX", tenant="globex")
    data = b"Globex confidential spec\nOutput voltage: 12 V\n"
    up = n0_src.upload(data, source_id="SRC-GLOBEX", tenant="globex", who="gp")
    assert up.status_code == 201
    a = rfq("RFQ-XF")
    a["file_refs"] = [up.json()["file_ref"]]
    a["source_sha256"] = up.json()["sha256"]
    res = n0_src.evaluate(a)
    assert res["decision"]["decision"] == "BLOCK" and "CROSS_TENANT_REFERENCE" in codes(res)


def test_tampered_file_sha_is_blocked(n0_src):
    data = b"Output voltage: 24 V\n"
    blob = n0_src.upload(data).json()
    a = rfq("RFQ-SHA")
    ref = dict(blob["file_ref"])
    ref["sha256"] = sha256_hex(b"something else")
    a["file_refs"], a["source_sha256"] = [ref], ref["sha256"]
    res = n0_src.evaluate(a)
    assert res["decision"]["decision"] == "BLOCK" and "SHA256_MISMATCH" in codes(res)


# ------------------------------------------------------------------------------------------------
# value_origin separation: CUSTOMER vs MEASURED vs DERIVED vs MODEL_EXTRACTED
# ------------------------------------------------------------------------------------------------
def test_derived_power_is_tagged_derived_and_customer_values_stay_customer(n0_src):
    res = n0_src.evaluate(rfq("RFQ-PRV-1"))
    reqs = {r["parameter"]: r for r in res["canonical_artifact"]["payload"]["requirements"]}
    p = reqs["nominal_output_power_w"]
    assert p["value"] == 120 and p["unit"] == "W" and p["value_origin"] == "DERIVED"
    assert p["derivation"]["formula"] == "nominal_output_power_w = output_voltage_v × output_current_a"
    assert p["derivation"]["inputs"] == ["output_voltage_v:R-002", "output_current_a:R-003"]
    assert reqs["nominal_input_power_w"]["value_origin"] == "DERIVED"
    assert round(reqs["nominal_input_power_w"]["value"], 2) == 130.43
    for name in ("output_voltage_v", "output_current_a", "output_power_w", "input_voltage_v"):
        assert reqs[name]["value_origin"] == "CUSTOMER"
    assert reqs["efficiency_pct"]["si_value"] == pytest.approx(0.92) and reqs["efficiency_pct"]["si_unit"] == "1"


def test_customer_cannot_claim_a_derived_quantity(n0_src):
    a = rfq("RFQ-PRV-2")
    a["payload"]["requirements"].append({"req_id": "R-099", "parameter": "nominal_output_power_w", "value": 120,
                                         "unit": "W", "value_origin": "CUSTOMER"})
    res = n0_src.evaluate(reseal(a))
    assert res["decision"]["decision"] == "BLOCK" and "ORIGIN_MISLABELED" in codes(res)


def test_submitted_derived_value_is_recomputed(n0_src):
    a = rfq("RFQ-PRV-3")
    a["payload"]["requirements"].append({"req_id": "R-098", "parameter": "nominal_output_power_w", "value": 125,
                                         "unit": "W", "value_origin": "DERIVED",
                                         "derivation": {"formula": "V*I", "inputs": ["R-002", "R-003"]}})
    res = n0_src.evaluate(reseal(a))
    assert res["decision"]["decision"] == "REWORK" and "DERIVED_VALUE_MISMATCH" in codes(res)


def test_measured_values_do_not_belong_in_an_rfq(n0_src):
    res = n0_src.evaluate(set_req(rfq("RFQ-PRV-4"), "output_current_a", value_origin="MEASURED"))
    assert res["decision"]["decision"] == "REWORK" and "ORIGIN_INVALID_FOR_TYPE" in codes(res)


def test_model_extracted_values_need_evidence_and_human_review(n0_src):
    a = rfq("RFQ-PRV-5")
    for r in a["payload"]["requirements"]:
        r["value_origin"] = "MODEL_EXTRACTED"
    res = n0_src.evaluate(reseal(a))
    assert res["decision"]["decision"] == "REWORK" and "EVIDENCE_MISSING" in codes(res)

    b = rfq("RFQ-PRV-6")
    for r in b["payload"]["requirements"]:
        r["value_origin"] = "MODEL_EXTRACTED"
        r["evidence_snippets"] = [{"source_ref": "payload", "text": f"{r['parameter']} = {r['value']}"}]
    res = n0_src.evaluate(reseal(b))
    assert res["decision"]["decision"] == "REVIEW" and codes(res) == {"HUMAN_CONFIRMATION_REQUIRED"}


def test_model_values_relabelled_as_customer_are_blocked(n0_src):
    a = rfq("RFQ-PRV-7")
    a["payload"]["extraction_model_run_id"] = "MR-does-not-matter"
    res = n0_src.evaluate(reseal(a))
    assert res["decision"]["decision"] == "BLOCK"
    assert "ORIGIN_MISLABELED" in codes(res) and "UPSTREAM_NOT_FOUND" in codes(res)


# ------------------------------------------------------------------------------------------------
# hard policy is final; scores are advisory
# ------------------------------------------------------------------------------------------------
def test_block_cannot_be_resolved_approved_or_overridden_by_feedback(n0_src):
    n0_src.source("SRC-SYN2", data_origin="SYNTHETIC")
    a = rfq("RFQ-HARD", source_id="SRC-SYN2")
    assert n0_src.evaluate(a)["decision"]["decision"] == "BLOCK"
    r = n0_src.resolve_review("RFQ-HARD")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BLOCK_NOT_OVERRIDABLE"
    assert n0_src.approve_release("RFQ-HARD").json()["detail"]["code"] == "BLOCK_NOT_OVERRIDABLE"
    fb = n0_src.post("/v1/gateway/feedback", "rex", json={"artifact_id": "RFQ-HARD", "revision": "A",
                                                          "override_reason": "looks fine to me"})
    assert fb.status_code == 200 and fb.json()["training_eligible"] is False
    view = n0_src.get("/v1/artifacts/RFQ-HARD").json()
    assert view["decision"]["decision"] == "BLOCK"
    assert n0_src.release("RFQ-HARD").json()["detail"]["code"] == "HARD_POLICY_BLOCK"


def test_quality_score_never_decides():
    from app.models import RuleViolation
    one_block = [RuleViolation(rule_id="N0-SAFE-X", code="X", severity="BLOCK", category="SAFETY", stage="N0.5",
                               message="x")]
    many_reviews = [RuleViolation(rule_id=f"R{i}", code="Y", severity="REVIEW", category="C", stage="N0.4",
                                  message="y") for i in range(12)]
    assert quality_score(many_reviews) == 0 and decide(many_reviews) == "REVIEW"
    assert decide(one_block) == "BLOCK"


def test_review_is_resolvable_only_by_another_reviewer(n0_src):
    a = rfq("RFQ-REV")
    a["payload"]["notes"] = "Please call our buyer at buyer@acme-controls.example tomorrow"
    res = n0_src.evaluate(reseal(a))
    assert res["decision"]["decision"] == "REVIEW" and codes(res) == {"PII_DETECTED"}
    assert res["quality_report"]["pii_findings"][0]["entity_type"] == "EMAIL_ADDRESS"
    assert n0_src.approve_release("RFQ-REV").json()["detail"]["code"] == "NOT_RELEASABLE"
    r = n0_src.resolve_review("RFQ-REV")
    assert r.status_code == 200, r.text
    resolved = r.json()["resulting_decision"]
    assert resolved["decision"] == "PASS" and resolved["resolved_by_approval_id"] == r.json()["approval_event"]["approval_id"]
    assert "N2" in resolved["allowed_destinations"]


# ------------------------------------------------------------------------------------------------
# revision control, upstream, stage gates, calibration, confidentiality
# ------------------------------------------------------------------------------------------------
def test_revision_is_immutable_and_rejected_resubmission_does_not_touch_stored_decision(n0_src):
    assert n0_src.evaluate(rfq("RFQ-IMM"))["decision"]["decision"] == "PASS"
    res = n0_src.evaluate(rfq("RFQ-IMM"))                                     # identical → re-evaluated, still PASS
    assert res["decision"]["decision"] == "PASS"
    changed = rfq("RFQ-IMM")
    changed["payload"]["quantity"] = 9999
    res = n0_src.evaluate(reseal(changed))
    assert res["decision"]["decision"] == "BLOCK" and "REVISION_IMMUTABLE" in codes(res)
    assert n0_src.get("/v1/artifacts/RFQ-IMM").json()["decision"]["decision"] == "PASS"


def test_revision_labels_are_normalized(n0_src):
    res = n0_src.evaluate(rfq("RFQ-REVN", revision="rev b"))
    assert res["decision"]["revision"] == "B" and res["canonical_artifact"]["revision"] == "B"
    bad = n0_src.evaluate(rfq("RFQ-REVN2", revision="B-draft"))
    assert bad["decision"]["decision"] == "REWORK" and "REVISION_FORMAT" in codes(bad)


def test_upstream_must_be_pinned_released_and_current(n0_src):
    n0_src.evaluate(drop_req(rfq("RFQ-UP"), "output_voltage_v"))            # REWORK
    ds = _datasheet("DS-1", upstream=["RFQ-UP@A"])
    res = n0_src.evaluate(ds)
    assert res["decision"]["decision"] == "BLOCK" and "UPSTREAM_NOT_RELEASED" in codes(res)
    res = n0_src.evaluate(_datasheet("DS-2", upstream=["RFQ-NOPE@A"]))
    assert "UPSTREAM_NOT_FOUND" in codes(res)


def _datasheet(aid: str, upstream=(), **kw) -> dict:
    payload = {"part_number": "PSU-24-120", "manufacturer": "Acme Power",
               "requirements": [{"req_id": "D-1", "parameter": "output_voltage_v", "value": 24, "unit": "V",
                                 "value_origin": "CUSTOMER"}]}
    a = {"tenant_id": "acme", "project_id": "PRJ-PSU-24V-120W", "artifact_id": aid, "artifact_type": "DATASHEET",
         "source_id": RFQ_SOURCE, "processing_purpose": "PRODUCT_DEFINITION", "release_stage": "CONCEPT",
         "revision": "A", "synthetic": False, "source_sha256": "0" * 64, "payload": payload,
         "source_artifact_ids": list(upstream), **kw}
    return reseal(a)


def test_model_output_cannot_pass_a_pilot_stage_gate(n0_src):
    n0_src.source("SRC-MODEL", permitted_use=("DESIGN_ENGINEERING",))
    payload = {"model_run_id": "MR-x", "task": "loss estimate", "output": {"loss_w": 9.6}}
    a = {"tenant_id": "acme", "project_id": "PRJ-1", "artifact_id": "MO-1", "artifact_type": "MODEL_OUTPUT",
         "source_id": "SRC-MODEL", "processing_purpose": "DESIGN_ENGINEERING", "release_stage": "PILOT",
         "revision": "A", "synthetic": False, "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload}
    res = n0_src.evaluate(a)
    assert res["decision"]["decision"] == "BLOCK" and "STAGE_GATE_VIOLATION" in codes(res)


def test_expired_calibration_blocks_measurements(n0_src):
    n0_src.source("SRC-LAB", permitted_use=("VERIFICATION",))
    payload = {"test_id": "T-1", "unit_id": "U-1", "measured_at": "2026-11-20T10:00:00Z", "result": "PASS",
               "instruments": [{"instrument_id": "DMM-7", "calibration_due": "2026-06-30T00:00:00Z"}],
               "measurements": [{"req_id": "M-1", "parameter": "output_voltage_v", "value": 24.02, "unit": "V",
                                 "value_origin": "MEASURED"}]}
    a = {"tenant_id": "acme", "project_id": "PRJ-1", "artifact_id": "TM-1", "artifact_type": "TEST_MEASUREMENT",
         "source_id": "SRC-LAB", "processing_purpose": "VERIFICATION", "release_stage": "PROTOTYPE",
         "revision": "A", "synthetic": False, "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload}
    res = n0_src.evaluate(a)
    assert res["decision"]["decision"] == "BLOCK" and "CALIBRATION_EXPIRED" in codes(res)


def test_clearance_and_purpose_are_enforced(n0_src):
    res = n0_src.evaluate(rfq("RFQ-CLR"), who="bob")                         # INTERNAL clearance, CONFIDENTIAL data
    assert res["decision"]["decision"] == "BLOCK" and "CONFIDENTIALITY_CLEARANCE" in codes(res)
    res = n0_src.evaluate(rfq("RFQ-PUR", processing_purpose="SOURCING"))
    assert "PURPOSE_NOT_PERMITTED" in codes(res) and res["rights_decision"]["status"] == "DENIED"


def test_non_commercial_license_is_evaluation_only(n0_src):
    n0_src.source("SRC-NC", license="CC-BY-NC-4.0", permitted_use=("RFQ_ANALYSIS", "EVALUATION"))
    res = n0_src.evaluate(rfq("RFQ-NC", source_id="SRC-NC"))
    assert res["decision"]["decision"] == "BLOCK" and "LICENSE_RESTRICTED" in codes(res)


def test_hazardous_voltage_needs_a_safety_standard(n0_src):
    a = set_req(rfq("RFQ-HV"), "input_voltage_v", value=230)
    a["payload"]["compliance_standards"] = []
    res = n0_src.evaluate(reseal(a))
    assert res["decision"]["decision"] == "REVIEW" and "SAFETY_REVIEW_REQUIRED" in codes(res)


def test_unit_problems_are_rework(n0_src):
    res = n0_src.evaluate(set_req(rfq("RFQ-U1"), "output_current_a", unit="V"))
    assert "UNIT_DIMENSION_MISMATCH" in codes(res) and res["decision"]["decision"] == "REWORK"
    res = n0_src.evaluate(set_req(rfq("RFQ-U2"), "output_current_a", unit="furlong"))
    assert "UNIT_UNKNOWN" in codes(res)
    mv = n0_src.evaluate(set_req(rfq("RFQ-U3"), "output_voltage_v", value=24000, unit="mV"))
    assert mv["decision"]["decision"] == "PASS"                               # 24000 mV × 5 A = 120 W


def test_node_service_may_only_speak_for_its_node(n0_src):
    res = n0_src.evaluate(rfq("RFQ-N2"), who="n2", origin_node="N3")
    assert res["decision"]["decision"] == "BLOCK" and "UNAUTHORIZED_PRODUCER" in codes(res)
    ok = n0_src.evaluate(rfq("RFQ-N2"), who="n2", origin_node="N2")
    assert ok["decision"]["decision"] == "PASS" and "N2" not in ok["decision"]["allowed_destinations"]


# ------------------------------------------------------------------------------------------------
# uploads, auth, audit integrity
# ------------------------------------------------------------------------------------------------
def test_upload_screening(n0_src, make_n0):
    from app.scanning import EICAR
    r = n0_src.upload(b"hello " + EICAR, "note.txt")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "MALWARE_DETECTED"
    r = n0_src.upload(b"MZ\x90\x00" + b"\x00" * 64, "rfq.pdf", mime="application/pdf")
    assert r.status_code == 415
    r = n0_src.upload(b"%PDF-1.7\n...", "rfq.txt")
    assert r.status_code == 415                                               # extension disagrees with bytes
    r = n0_src.upload(b"<html><script>alert(1)</script></html>", "x.txt")
    assert r.status_code == 415
    r = n0_src.upload(b"abc", "a.txt", sha256="f" * 64)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "SHA256_MISMATCH"
    small = make_n0(max_upload_bytes=16)
    small.source()
    assert small.upload(b"x" * 64, "big.txt").status_code == 413
    strict = make_n0(require_av=True)
    strict.source()
    assert strict.upload(b"plain text", "a.txt").status_code == 503
    assert strict.c.get("/health").status_code == 503


def test_authentication_and_authorization(n0_src):
    art = load_example("valid_rfq.json")
    assert n0_src.c.post("/v1/gateway/evaluate", json={"artifact": art}).status_code == 401
    assert n0_src.c.post("/v1/gateway/evaluate", json={"artifact": art},
                         headers={"Authorization": "Bearer nope"}).status_code == 401
    assert n0_src.post("/v1/gateway/evaluate", "aud", json={"artifact": art}).status_code == 403
    body = {"source_id": "SRC-G", "tenant_id": "globex", "publisher": "x", "license": "MIT",
            "permitted_use": ["RFQ_ANALYSIS"]}
    assert n0_src.post("/v1/sources/register", "sam", json=body).status_code == 403
    assert n0_src.get(f"/v1/sources/acme/{RFQ_SOURCE}", "gp").status_code == 403


def test_audit_chain_verifies_and_tables_are_append_only(n0_src):
    n0_src.evaluate(load_example("valid_rfq.json"))
    n0_src.evaluate(load_example("invalid_rfq.json"))
    n0_src.post("/v1/gateway/evaluate", "aud", json={"artifact": load_example("valid_rfq.json")})   # refused → audited
    v = n0_src.get("/v1/audit/verify").json()
    assert v["audit"]["ok"] and v["approvals"]["ok"] and v["audit"]["length"] >= 5
    events = n0_src.get("/v1/audit/events").json()
    assert any(e["event_type"] == "GATEWAY_EVALUATION_REFUSED" for e in events)
    db = n0_src.app.state.db
    for stmt in ("UPDATE audit_events SET actor_id='mallory'", "DELETE FROM decisions",
                 "UPDATE approval_events SET decision='APPROVED'", "DELETE FROM lineage_edges"):
        with pytest.raises(Exception, match="append-only"):
            with db.engine.begin() as conn:
                conn.execute(text(stmt))


def test_store_is_write_once(n0_src):
    from app.storage import ImmutableObjectError
    store = n0_src.app.state.store
    key = store.key_for("acme", sha256_hex(b"a"))
    store.put_immutable("quarantine", key, b"a")
    store.put_immutable("quarantine", key, b"a")                              # identical rewrite: no-op
    with pytest.raises(ImmutableObjectError):
        store.put_immutable("quarantine", key, b"b")


def test_duplicate_content_under_new_id_needs_review(n0_src):
    a = rfq("RFQ-DUP-1")
    assert n0_src.evaluate(a)["decision"]["decision"] == "PASS"
    b = copy.deepcopy(a)
    b["artifact_id"] = "RFQ-DUP-2"
    res = n0_src.evaluate(b)
    assert res["decision"]["decision"] == "REVIEW" and "DUPLICATE_CONTENT" in codes(res)
