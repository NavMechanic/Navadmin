"""End-to-end: RFQ → N0 → N2 handoff with ApprovalEvent, the extraction path, downstream pinning,
feedback/training gates, the vendor boundary round trip, and lineage."""
from __future__ import annotations

import copy

from app.rules import canonical_json, sha256_hex
from app.vendor_contracts import result_checksum
from conftest import EXAMPLES, RFQ_SOURCE, codes, load_example, reseal, rfq


def _relations(graph: dict) -> set[tuple[str, str, str]]:
    return {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}


def test_valid_rfq_is_released_to_n2_end_to_end(n0_src):
    art = load_example("valid_rfq.json")
    res = n0_src.evaluate(art)
    assert res["decision"]["decision"] == "PASS" and "N2" in res["decision"]["allowed_destinations"]
    aid = art["artifact_id"]

    r = n0_src.release(aid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "APPROVAL_REQUIRED"
    assert n0_src.approve_release(aid, who="alice").status_code == 403           # producer is not a release manager
    appr = n0_src.approve_release(aid)
    assert appr.status_code == 200, appr.text
    event = appr.json()["approval_event"]
    assert event["scope"] == "RELEASE" and event["exact_revision"] == "A" and event["approver_role"] == "release_manager"

    assert n0_src.release(aid, targets=("N7",)).json()["detail"]["code"] == "DESTINATION_NOT_ALLOWED"
    assert n0_src.release(aid, source_node="N3").json()["detail"]["code"] == "SOURCE_NODE_MISMATCH"
    assert n0_src.release(aid, who="alice").status_code == 403
    rel = n0_src.release(aid, targets=("N2",), approval_id=event["approval_id"])
    assert rel.status_code == 200, rel.text
    m = rel.json()
    assert m["status"] == "RELEASED" and m["target_nodes"] == ["N2"] and m["approval_id"] == event["approval_id"]
    assert m["decision_id"] == res["decision"]["decision_id"] and m["released_by"] == "rm"
    assert m["payload_sha256"] == res["provenance"]["payload_sha256"] and len(m["manifest_sha256"]) == 64

    view = n0_src.get(f"/v1/artifacts/{aid}").json()
    assert {x["status"] for x in view["releases"]} == {"RELEASABLE", "RELEASED"}
    graph = n0_src.get(f"/v1/artifacts/{aid}/lineage").json()
    node = f"artifact:{aid}@A"
    rels = _relations(graph)
    assert (f"source:{RFQ_SOURCE}", node, "SOURCE_OF") in rels
    assert (f"release:{m['manifest_id']}", "node:N2", "DELIVERED_TO") in rels
    assert (f"approval:{event['approval_id']}", node, "APPROVES") in rels
    assert any(r[2] == "EVALUATED_AS" for r in rels) and any(r[2] == "PAYLOAD_OF" for r in rels)
    kinds = {n["kind"] for n in graph["nodes"]}
    assert {"source", "artifact", "blob", "decision", "approval", "release", "node"} <= kinds
    verify = n0_src.get("/v1/audit/verify").json()
    assert verify["audit"]["ok"] and verify["approvals"]["ok"]


def test_four_eyes_on_release_approval(n0_src):
    n0_src.evaluate(rfq("RFQ-4E"), who="pat")
    r = n0_src.approve_release("RFQ-4E", who="pat")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FOUR_EYES"
    assert n0_src.approve_release("RFQ-4E").status_code == 200


def test_invalid_rfq_is_refused_then_fixed_revision_is_released(n0_src):
    bad = load_example("invalid_rfq.json")
    aid = bad["artifact_id"]
    assert n0_src.evaluate(bad)["decision"]["decision"] == "REWORK"
    assert n0_src.approve_release(aid).json()["detail"]["code"] == "NOT_RELEASABLE"
    assert n0_src.release(aid).json()["detail"]["code"] == "NOT_RELEASABLE"

    fixed = copy.deepcopy(load_example("valid_rfq.json"))
    fixed.update(artifact_id=aid, revision="B")
    fixed["payload"]["rfq_id"] = aid
    reseal(fixed)
    res = n0_src.evaluate(fixed)
    assert res["decision"]["decision"] == "PASS"
    assert n0_src.release(aid, "A").json()["detail"]["code"] == "SUPERSEDED_REVISION"
    assert n0_src.approve_release(aid, "B").status_code == 200
    assert n0_src.release(aid, "B").status_code == 200
    rels = _relations(n0_src.get(f"/v1/artifacts/{aid}/lineage").json())
    assert (f"artifact:{aid}@A", f"artifact:{aid}@B", "SUPERSEDED_BY") in rels
    assert n0_src.get(f"/v1/artifacts/{aid}", params={"revision": "A"}).json()["superseded"] is True


def test_approval_must_postdate_the_current_decision(n0_src):
    a = rfq("RFQ-STALE")
    n0_src.evaluate(a)
    assert n0_src.approve_release("RFQ-STALE").status_code == 200
    n0_src.evaluate(a)                                                     # re-evaluated after the approval
    assert n0_src.release("RFQ-STALE").json()["detail"]["code"] == "APPROVAL_STALE"
    assert n0_src.approve_release("RFQ-STALE", decision="REJECTED").status_code == 200
    assert n0_src.release("RFQ-STALE").json()["detail"]["code"] == "APPROVAL_REJECTED"
    assert n0_src.approve_release("RFQ-STALE").status_code == 200
    assert n0_src.release("RFQ-STALE").status_code == 200


def test_extracted_rfq_needs_human_review_before_reaching_n2(n0_src):
    doc = (EXAMPLES / "rfq_email.txt").read_bytes()
    blob = n0_src.upload(doc, "rfq_email.txt").json()
    ext = n0_src.post("/v1/extraction/run", json={"tenant_id": "acme", "project_id": "PRJ-PSU-24V-120W",
                                                  "source_id": RFQ_SOURCE, "blob_id": blob["blob_id"]}).json()
    draft, run = ext["draft"], ext["model_run"]
    payload = {"rfq_id": "RFQ-EXT-1", "customer_name": draft["customer_name"],
               "product_category": draft["product_category"], "quantity": draft["quantity"],
               "compliance_standards": ["IEC 62368-1"], "requirements": draft["requirements"],
               "extraction_model_run_id": run["run_id"]}
    art = {"tenant_id": "acme", "project_id": "PRJ-PSU-24V-120W", "artifact_id": "RFQ-EXT-1",
           "artifact_type": "RFQ", "source_id": RFQ_SOURCE, "processing_purpose": "RFQ_ANALYSIS",
           "release_stage": "CONCEPT", "revision": "A", "synthetic": False, "source_sha256": blob["sha256"],
           "file_refs": [blob["file_ref"]], "payload": payload}
    res = n0_src.evaluate(art)
    assert res["decision"]["decision"] == "REVIEW"
    assert {"HUMAN_CONFIRMATION_REQUIRED", "PII_DETECTED"} == codes(res)
    assert res["provenance"]["model_run_ids"] == [run["run_id"]]
    assert res["audit_event"]["model_runs"][0]["prompt_revision"] == run["prompt_revision"]
    assert n0_src.release("RFQ-EXT-1").json()["detail"]["code"] == "NOT_RELEASABLE"

    resolved = n0_src.resolve_review("RFQ-EXT-1")
    assert resolved.status_code == 200 and resolved.json()["resulting_decision"]["decision"] == "PASS"
    assert n0_src.approve_release("RFQ-EXT-1").status_code == 200
    rel = n0_src.release("RFQ-EXT-1")
    assert rel.status_code == 200 and rel.json()["status"] == "RELEASED"
    view = n0_src.get("/v1/artifacts/RFQ-EXT-1").json()
    origins = {r["parameter"]: r["value_origin"] for r in view["canonical_artifact"]["payload"]["requirements"]}
    assert origins["output_voltage_v"] == "MODEL_EXTRACTED" and origins["nominal_output_power_w"] == "DERIVED"
    assert view["canonical_artifact"]["file_refs"][0]["uri"].startswith("n0://released/acme/")
    graph = n0_src.get("/v1/artifacts/RFQ-EXT-1/lineage").json()
    rels = _relations(graph)
    assert (f"model_run:{run['run_id']}", "artifact:RFQ-EXT-1@A", "EXTRACTED_INTO") in rels
    assert (f"blob:{blob['blob_id']}", f"model_run:{run['run_id']}", "INPUT_TO") in rels


def test_downstream_node_pins_a_released_upstream(n0_src):
    assert n0_src.evaluate(load_example("valid_rfq.json"))["decision"]["decision"] == "PASS"
    payload = {"part_number": "PSU-24-120", "manufacturer": "Acme Power", "datasheet_revision": "1.0",
               "requirements": [{"req_id": "D-1", "parameter": "output_voltage_v", "value": 24, "unit": "V",
                                 "value_origin": "CUSTOMER"}]}
    ds = {"tenant_id": "acme", "project_id": "PRJ-PSU-24V-120W", "artifact_id": "DS-PSU-24-120",
          "artifact_type": "DATASHEET", "source_id": RFQ_SOURCE, "processing_purpose": "PRODUCT_DEFINITION",
          "release_stage": "CONCEPT", "revision": "A", "synthetic": False,
          "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload,
          "source_artifact_ids": ["RFQ-2026-0042@A"]}
    res = n0_src.evaluate(ds, who="n2", origin_node="N2")
    assert res["decision"]["decision"] == "PASS" and res["decision"]["allowed_destinations"] == ["N3"]
    assert res["provenance"]["upstream"] == ["RFQ-2026-0042@A"]
    rels = _relations(n0_src.get("/v1/artifacts/RFQ-2026-0042/lineage").json())
    assert ("artifact:RFQ-2026-0042@A", "artifact:DS-PSU-24-120@A", "UPSTREAM_OF") in rels


def test_feedback_training_requires_real_data_and_training_rights(n0_src):
    n0_src.source("SRC-SIM", data_origin="SYNTHETIC")
    n0_src.evaluate(rfq("RFQ-SIM", source_id="SRC-SIM", synthetic=True))
    label = {"label": "power-balance-ok", "use_for_training": True}
    r = n0_src.post("/v1/gateway/feedback", "rex", json={"artifact_id": "RFQ-SIM", "revision": "A",
                                                         "training_label": label})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "SYNTHETIC_NOT_TRAINABLE"

    n0_src.evaluate(rfq("RFQ-REAL"))
    r = n0_src.post("/v1/gateway/feedback", "rex", json={"artifact_id": "RFQ-REAL", "revision": "A",
                                                         "training_label": label})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "TRAINING_RIGHTS_MISSING"

    n0_src.source("SRC-TRAIN", permitted_use=("RFQ_ANALYSIS", "MODEL_TRAINING"))
    n0_src.evaluate(rfq("RFQ-TRAIN", source_id="SRC-TRAIN"))
    body = {"artifact_id": "RFQ-TRAIN", "revision": "A", "training_label": label,
            "corrections": [{"req_id": "R-005", "field": "value", "corrected_value": 93, "reason": "per rev 2 spec"}]}
    r = n0_src.post("/v1/gateway/feedback", "rex", json=body)
    assert r.status_code == 200, r.text
    fb = r.json()
    assert fb["training_eligible"] is True and fb["approval_event"]["scope"] == "FEEDBACK"
    assert n0_src.get("/v1/artifacts/RFQ-TRAIN").json()["decision"]["decision"] == "PASS"   # labels never re-decide
    assert n0_src.post("/v1/gateway/feedback", "alice", json=body).status_code == 403


def test_vendor_boundary_round_trip(n0_src):
    n0_src.source("SRC-VEND", permitted_use=("RFQ_ANALYSIS", "VENDOR_TRANSMISSION"),
                  allowed_vendor_ids=["VND-THERMAL"])
    n0_src.evaluate(rfq("RFQ-VND", source_id="SRC-VEND"))
    wp = {"work_package_id": "WP-THERM-1", "tenant_id": "acme", "project_id": "PRJ-PSU-24V-120W",
          "vendor_id": "VND-THERMAL", "connector_type": "FILE_PORTAL", "approved_input_ids": ["RFQ-VND@A"],
          "budget_cap": {"amount": "1200", "currency": "EUR"},
          "expected_outputs": [{"name": "thermal_report", "format": "application/json"}]}
    d = n0_src.post("/v1/vendor/work-packages", "rm", json=wp).json()
    assert d["authorized"] is True and d["authorized_inputs"] == ["RFQ-VND@A"]
    assert n0_src.post("/v1/vendor/work-packages", "rm", json=wp).status_code == 409        # immutable
    other = dict(wp, work_package_id="WP-OTHER", vendor_id="VND-UNKNOWN")
    assert n0_src.post("/v1/vendor/work-packages", "rm", json=other).json()["authorized"] is False
    assert n0_src.post("/v1/vendor/work-packages", "alice", json=dict(wp, work_package_id="WP-X")).status_code == 403

    report = b'{"hotspot_c": 71.5, "ambient_c": 40}'
    up = n0_src.upload(report, "thermal_report.json", source_id="SRC-VEND", mime="application/json").json()
    result = {"work_package_id": "WP-THERM-1", "native_file_uris": [up["file_ref"]["uri"]],
              "result_manifest": {"thermal_report": up["sha256"]}, "checksum_sha256": result_checksum([up["sha256"]]),
              "tool_version": "thermsim 4.2.1"}
    ok = n0_src.post("/v1/vendor/results", json=result).json()
    assert ok["status"] == "ACCEPTED" and ok["checksum_verified"] is True
    tampered = n0_src.post("/v1/vendor/results", json=dict(result, checksum_sha256="0" * 64)).json()
    assert tampered["status"] == "REJECTED" and tampered["violated_rules"][0]["code"] == "SHA256_MISMATCH"
    unknown = dict(result, native_file_uris=[f"n0://quarantine/acme/{'1' * 64}"])
    assert n0_src.post("/v1/vendor/results", json=unknown).json()["violated_rules"][0]["code"] == "BLOB_NOT_CLEARED"
    blocked = n0_src.post("/v1/vendor/results", json=dict(result, work_package_id="WP-OTHER"))
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "WORK_PACKAGE_NOT_AUTHORIZED"
    rels = _relations(n0_src.get("/v1/artifacts/RFQ-VND/lineage").json())
    assert ("artifact:RFQ-VND@A", "work_package:WP-THERM-1", "INPUT_TO") in rels
    assert any(r[0] == "work_package:WP-THERM-1" and r[2] == "PRODUCED" for r in rels)


def test_lineage_is_tenant_scoped_and_excludes_siblings(n0_src):
    n0_src.evaluate(rfq("RFQ-SIB-1"))
    n0_src.evaluate(rfq("RFQ-SIB-2"))
    graph = n0_src.get("/v1/artifacts/RFQ-SIB-1/lineage").json()
    assert not any("RFQ-SIB-2" in n["id"] for n in graph["nodes"])
    assert n0_src.c.get("/v1/artifacts/RFQ-SIB-1/lineage", headers=n0_src.h("gp")).status_code == 404
