"""N4 through the API: design intake (N4.1), plan + traceability (N4.2), PFMEA / CAPA (N4.3), evidence
(N4.4), assessment (N4.5), review against the exact evidence hash (N4.6), N0-checked release to
N8 / N6 / N7 (N4.7) and the minimal N8 receiver. Fictional data throughout."""
from __future__ import annotations

from pathlib import Path

import pytest

from n4_support import SAFETY_IDS, codes, pfmea_entries, plan_cases


@pytest.fixture
def ready(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    return flow4, i


def test_intake_locks_released_designs_and_prd_hazards(ready):
    flow, i = ready
    assert [d["scope"] for d in i["design_refs"]] == ["schematic_pcb", "circuit_simulation"]
    assert all(d["artifact"].endswith("@B") and len(d["payload_sha256"]) == 64 for d in i["design_refs"])
    assert i["design_revision"] == i["pcb_revision"] == "B" and i["prd_artifact"] == "PRD-PSU-120W@A"
    assert {r["req_id"] for r in i["requirements"] if r["safety_critical"]} == SAFETY_IDS
    assert {h["hazard_id"] for h in i["hazards"]} == {"H-1", "H-2", "H-3"}
    assert len(i["simulation_corners"]) == 4 and "NOT product safety certification" in i["approval_scope"]
    assert flow.intake()["intake_id"] == i["intake_id"]                       # idempotent lock


def test_intake_requires_every_prd_scope_released_to_n4(flow4):
    flow4.sources()
    flow4.n4_sources()
    flow4.rfq_to_n2(flow4.rfq_art())
    body = flow4.prd_body(scopes=["circuit_simulation"])
    flow4.c.post("/v1/n2/prds", "pm", json=body)
    flow4.c.post("/v1/n2/prds/PSU-120W/revisions/A/release", "pa", json={"reason": "PRD approved for design"})
    b = flow4.baseline()
    flow4.scope(b["baseline_id"], "circuit_simulation")
    r = flow4.intake(expect=409)
    assert r["detail"]["code"] == "DESIGN_NOT_RELEASED_TO_N4"
    assert flow4.release(b["baseline_id"], "circuit_simulation", targets=("N4",)).status_code == 201
    assert flow4.intake()["design_revision"] == "B"


def test_intake_refuses_an_unapproved_release_source(ready):
    flow, _ = ready
    flow.c.source("SRC-NOLIFE", permitted_use=("COMMERCIAL",))
    r = flow.c.post("/v1/n4/intakes", "te", json={"prd_id": "PSU-120W", "project_id": "PRJ-PSU-120W",
                                                  "release_source_id": "SRC-NOLIFE"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "RELEASE_SOURCE_NOT_APPROVED"


def test_tampered_design_payload_bytes_break_the_intake(flow4):
    flow4.designed()
    art = flow4.c.get("/v1/artifacts/DSN-PSU-120W-schematic_pcb", "aud").json()
    sha = [m for m in art["releases"] if m["status"] == "RELEASED"][0]["payload_sha256"]
    blob = Path(flow4.c.app.state.settings.storage_root, "quarantine", "acme", sha)
    blob.chmod(0o644)
    blob.write_bytes(blob.read_bytes().replace(b'"B"', b'"C"', 1))           # out-of-band edit of the design
    r = flow4.intake(expect=409)
    assert r["detail"]["code"] == "DESIGN_SIGNATURE_INVALID" and "do not match" in r["detail"]["message"]


def test_evidence_sources_must_be_approved_for_verification(ready):
    flow, i = ready
    p = flow.n4_plan(i["intake_id"])
    r = flow.bench(p["plan_id"], "T-01", source="SRC-VER")                  # LIFECYCLE / COMMERCIAL / FINANCE only
    assert r.status_code == 409 and r.json()["detail"]["code"] == "EVIDENCE_SOURCE_PURPOSE"
    r = flow.bench(p["plan_id"], "T-01", source="SRC-NOPE")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "EVIDENCE_SOURCE_NOT_APPROVED"


def test_plan_test_ids_must_be_unique(ready):
    flow, i = ready
    cases = plan_cases()[:2]
    cases[1]["test_id"] = cases[0]["test_id"]
    r = flow.c.post(f"/v1/n4/intakes/{i['intake_id']}/plans", "te", json={"title": "dup ids", "test_cases": cases})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "DUPLICATE_TEST_ID"


def test_traceability_reaches_100_percent_safety_critical_coverage(ready):
    flow, i = ready
    p = flow.n4_plan(i["intake_id"])
    m = flow.c.get(f"/v1/n4/plans/{p['plan_id']}/traceability", "aud").json()
    assert m["complete"] and m["safety_critical_coverage_pct"] == "100.0" and m["safety_critical_total"] == 5
    row = next(r for r in m["rows"] if r["requirement_id"] == "R-008")
    assert row["test_ids"] == ["T-09"] and row["methods"] == ["ACCREDITED_LAB"] and row["hazard_ids"] == ["H-1"]


def test_traceability_reports_unmapped_sim_only_and_unknown_references(ready):
    flow, i = ready
    cases = [c for c in plan_cases() if c["test_id"] not in ("T-09", "T-08")]
    cases[0]["requirement_ids"].append("R-404")
    cases.append({**plan_cases()[13], "test_id": "T-15", "requirement_ids": ["R-007"]})          # SIM only for R-007
    p = flow.n4_plan(i["intake_id"], cases)
    m = flow.c.get(f"/v1/n4/plans/{p['plan_id']}/traceability", "te").json()
    assert not m["complete"] and set(m["uncovered_safety_critical"]) == {"R-007", "R-008"}
    assert m["unknown_requirement_refs"] == ["R-404"] and m["safety_critical_coverage_pct"] == "60.0"
    assert "R-006" in m["uncovered_other"]


def test_sop_revisions_need_a_separate_approver(ready):
    flow, _ = ready
    flow.method("SOP-NEW-001", "A", "BENCH_ATE", approve=False)
    r = flow.c.post("/v1/n4/methods/SOP-NEW-001/revisions/A/approve", "te")
    assert r.status_code == 403
    ok = flow.c.post("/v1/n4/methods/SOP-NEW-001/revisions/A/approve", "qr").json()
    assert ok["status"] == "APPROVED" and ok["approved_by"] == "qr" and ok["document_sha256"]
    assert flow.c.post("/v1/n4/methods/SOP-NEW-001/revisions/A/approve", "qr2").status_code == 409


def test_calibration_records_need_a_registered_instrument_and_a_later_due_date(ready):
    flow, _ = ready
    import json
    body = {"calibration_id": "CAL-X", "instrument_id": "NOPE", "certificate_number": "C", "calibration_lab": "L",
            "calibrated_at": "2026-01-01T00:00:00Z", "calibration_due": "2027-01-01T00:00:00Z"}
    assert flow.c.post("/v1/n4/calibrations", "li", data={"body": json.dumps(body)}).status_code == 404
    bad = {**body, "instrument_id": "DMM-1", "calibration_due": "2025-01-01T00:00:00Z"}
    assert flow.c.post("/v1/n4/calibrations", "li", data={"body": json.dumps(bad)}).status_code == 422


def test_pfmea_is_human_rated_and_labelled_as_a_custom_heuristic(ready):
    flow, i = ready
    p = flow.n4_plan(i["intake_id"])
    pf = flow.pfmea(p["plan_id"]).json()
    assert pf["prioritization_method"] == "CUSTOM_ATTENTION_HEURISTIC" and "NOT AIAG-VDA" in pf["prioritization_label"]
    e = pf["entries"][0]
    assert e["rated_by"] == "te" and e["rpn_illustrative"] == 40 and e["attention_level"] == "HIGH"
    assert e["action_priority"] is None and "illustrative" in e["attention_basis"]
    assert flow.pfmea(p["plan_id"]).json()["revision"] == 2


def test_capa_effectiveness_is_verified_by_someone_else_against_accepted_evidence(ready):
    flow, i = ready
    p = flow.n4_plan(i["intake_id"])
    c = flow.c.post("/v1/n4/capas", "te", json={"capa_id": "CAPA-1", "plan_id": p["plan_id"], "title": "Y-cap mix-up",
                                                "pfmea_entry_ids": ["FM-02"], "root_cause": "feeder labelling",
                                                "corrective_action": "barcode feeder check", "owner": "te"})
    assert c.status_code == 201 and c.json()["status"] == "OPEN"
    ev = flow.bench(p["plan_id"], "T-01").json()
    body = {"evidence_id": ev["evidence_id"], "effective": True, "notes": "re-test passed"}
    assert flow.c.post("/v1/n4/capas/CAPA-1/verify", "te", json=body).status_code == 403
    v = flow.c.post("/v1/n4/capas/CAPA-1/verify", "qr", json=body).json()
    assert v["status"] == "VERIFIED_EFFECTIVE" and v["verification_evidence_sha256"] == ev["evidence_sha256"]


def test_fully_evidenced_plan_passes_every_test(flow4):
    i, p, a = flow4.verified()
    assert [v["verdict"] for v in a["test_verdicts"]] == ["PASS"] * 14
    assert codes(a, "BLOCK") == set() and codes(a, "REWORK") == set()
    assert a["traceability"]["complete"] and a["unit_serials"] == ["PSU-0001"]
    t14 = next(v for v in a["test_verdicts"] if v["test_id"] == "T-14")                # N3 SPICE corners
    assert len(t14["measurements"]) == 4 and {m["provenance"] for m in t14["measurements"]} == {"SIMULATION"}
    t13 = next(v for v in a["test_verdicts"] if v["test_id"] == "T-13")
    assert t13["attribute_inspections"][0]["human_verdict"] == "ACCEPT" and t13["measurements"] == []


def test_approval_binds_the_exact_evidence_hash_and_design_revision(flow4):
    i, p, a = flow4.verified()
    r = flow4.n4_review(a, evidence_sha256="0" * 64)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "EVIDENCE_HASH_MISMATCH"
    r = flow4.n4_review(a, design_revision="C")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DESIGN_REVISION_MISMATCH"
    ok = flow4.n4_review(a).json()
    assert ok["evidence_sha256"] == a["evidence_sha256"] and ok["reviewer_role"] == "test_authority"


def test_new_evidence_after_assessment_makes_the_approval_stale(flow4):
    i, p, a = flow4.verified()
    flow4.bench(p["plan_id"], "T-01", value="24.01", serial="PSU-0002")
    a2 = flow4.n4_assess(p["plan_id"])
    assert a2["evidence_sha256"] != a["evidence_sha256"]
    r = flow4.n4_review(a)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_ASSESSMENT"


def test_release_needs_an_approval(flow4):
    i, p, a = flow4.verified()
    r = flow4.n4_release(p["plan_id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "APPROVAL_REQUIRED"


def test_release_sends_n0_checked_packages_to_n8_n6_n7(flow4):
    i, p, a = flow4.verified()
    assert flow4.n4_review(a).status_code == 201
    rel = flow4.n4_release(p["plan_id"])
    assert rel.status_code == 201, rel.text
    pk = {x["target"]: x for x in rel.json()["packages"]}
    assert {t: x["status"] for t, x in pk.items()} == {"N8": "RELEASED", "N6": "RELEASED", "N7": "RELEASED"}
    n8 = flow4.c.get(f"/v1/artifacts/{pk['N8']['artifact_id']}", "aud").json()
    art = n8["canonical_artifact"]
    assert art["processing_purpose"] == "LIFECYCLE" and n8["decision"]["allowed_destinations"] == ["N8"]
    assert set(art["source_artifact_ids"]) == {d["artifact"] for d in i["design_refs"]} | {"PRD-PSU-120W@A"}
    pl = art["payload"]
    assert pl["verification_status"] == "VERIFIED" and pl["evidence_sha256"] == a["evidence_sha256"]
    assert pl["approved_by"] == "ta" and "ta" not in pl["evidence_submitters"] and pl["control_plan"]
    assert all(m["verdict"] == "PASS" and m["calibration_due"] for m in pl["measurements"] if m["method"] != "SIM")
    assert {t["requirement_id"] for t in pl["traceability"] if t["safety_critical"] and t["verdict"] == "VERIFIED"} \
        == SAFETY_IDS
    assert pl["lab_certificates"][0]["certificate_number"] == "LAB-2026-0042"


def test_n6_claims_exclude_reliability_and_n7_gets_effort_figures(flow4):
    i, p, a = flow4.verified()
    flow4.n4_review(a)
    pk = {x["target"]: x for x in flow4.n4_release(p["plan_id"]).json()["packages"]}
    n6 = flow4.c.get(f"/v1/artifacts/{pk['N6']['artifact_id']}", "aud").json()["canonical_artifact"]["payload"]
    assert any("isolation_voltage_v" in c for c in n6["commercial"]["claims"])
    assert not any("mtbf" in c.lower() for c in n6["commercial"]["claims"])
    n7 = flow4.c.get(f"/v1/artifacts/{pk['N7']['artifact_id']}", "aud").json()["canonical_artifact"]["payload"]
    assert n7["finance"]["test_cases"] == 14 and n7["finance"]["accredited_lab_reports"] == 1


def test_n8_draft_is_a_preview_never_a_release(flow4):
    i, p, a = flow4.verified()
    d = flow4.c.get(f"/v1/n4/plans/{p['plan_id']}/n8-draft", "te").json()
    assert d["status"] == "DRAFT_NOT_RELEASED" and d["payload"]["release_id"] == "N4R-DRAFT"
    assert flow4.c.get("/v1/artifacts/VER-PSU-120W-N8", "aud").status_code == 404


def test_n8_ingests_only_released_verification_packages(flow4):
    i, p, a = flow4.verified()
    flow4.n4_review(a)
    pk = {x["target"]: x for x in flow4.n4_release(p["plan_id"]).json()["packages"]}
    assert flow4.c.post("/v1/n8/intake", "lo", json={"artifact_id": pk["N6"]["artifact_id"],
                                                     "revision": "A"}).status_code == 409   # released to N6, not N8
    assert flow4.c.post("/v1/n8/intake", "lo", json={"artifact_id": "DSN-PSU-120W-schematic_pcb",
                                                     "revision": "B"}).status_code == 404
    body = {"artifact_id": pk["N8"]["artifact_id"], "revision": pk["N8"]["revision"]}
    assert flow4.c.post("/v1/n8/intake", "te", json=body).status_code == 403
    got = flow4.c.post("/v1/n8/intake", "lo", json=body)
    assert got.status_code == 201 and got.json()["units"] == ["PSU-0001"]
    assert flow4.c.post("/v1/n8/intake", "lo", json=body).json()["detail"]["code"] == "ALREADY_INGESTED"
    unit = flow4.c.get("/v1/n8/units/PSU-0001", "lo").json()["history"][0]
    assert unit["design_revision"] == "B" and unit["verification_evidence_sha256"] == a["evidence_sha256"]
    mons = {m["characteristic"]: m for m in flow4.c.get("/v1/n8/monitors", "lo").json()}
    assert mons["output_voltage"]["upper_limit"] == "24.5" and mons["output_voltage"]["guard_band"]
    assert mons["hipot_leakage_current"]["reaction_plan"] == "scrap the unit, stop the line"


def test_status_views_track_intake_plan_and_release(flow4):
    i, p, a = flow4.verified()
    flow4.n4_review(a)
    rel = flow4.n4_release(p["plan_id"]).json()
    st = flow4.c.get(f"/v1/n4/subjects/plan/{p['plan_id']}/status", "aud").json()
    assert [h["status"] for h in st["history"]] == ["PLANNED", "ASSESSED", "REVIEW_APPROVED"]
    assert flow4.c.get(f"/v1/n4/subjects/release/{rel['release_id']}/status", "aud").json()["status"] == "RELEASED"
    assert flow4.c.get(f"/v1/n4/subjects/intake/{i['intake_id']}/status", "aud").json()["status"] == "LOCKED"


def test_plan_view_lists_evidence_adjudications_and_pfmea(flow4):
    i, p, a = flow4.verified()
    v = flow4.c.get(f"/v1/n4/plans/{p['plan_id']}", "aud").json()
    adapters = sorted({e["adapter"] for e in v["evidence"]})
    assert adapters == ["ACCREDITED_LAB", "AOI_VISION", "CSV_OFFLINE", "SCPI_VISA"]
    assert all(f["uri"].startswith("n0://quarantine/acme/") for e in v["evidence"] for f in e["files"])
    assert v["pfmea"]["entries"][0]["entry_id"] == "FM-01" and len(v["adjudications"]) == 1


def test_ai_layer_status_is_explicit(flow4):
    s = flow4.c.get("/v1/n4/status", "aud").json()["ai"]
    assert s["deterministic_policy_engine"] == "ACTIVE" and s["llm_rag_suggestions"] == "OPT_IN (disabled)"
    assert s["slm_extractor"] == "PLANNED" and s["vision_release_decisions"] == "DEFERRED_GATED"
    assert s["reliability"] == "RESEARCH_ONLY_STATISTICAL_ILLUSTRATION" and "NOT AIAG-VDA" in s["pfmea_prioritization"]
    assert len(pfmea_entries()) == 4
