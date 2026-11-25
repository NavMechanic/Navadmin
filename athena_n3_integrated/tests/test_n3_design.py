"""N3 design intelligence through the API: baseline lock (N3.1), planning (N3.2), routing (N3.3),
controlled intake and tool jobs (N3.4), verification (N3.5), decision support and the design-authority
gate (N3.6), and release (N3.7). Fake tools; all design data synthetic."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.repository import Repository
from n3_support import EICAR_BOM, codes, design_files, gen, manifest, sha


def test_baseline_locks_a_verified_n2_prd_release(flow):
    b = flow.ready()
    rel = flow.c.get("/v1/n2/prds/PSU-120W", "de").json()["releases"][0]
    assert b["prd_artifact"] == "PRD-PSU-120W@A" and b["prd_release_id"] == rel["release_id"]
    assert b["prd_payload_sha256"] == rel["payload_sha256"] and b["prd_approval_hash"] == rel["approval_event_hash"]
    assert b["design_scopes"] == ["schematic_pcb", "circuit_simulation", "mechanical_thermal"]
    assert b["constraints"]["ambient_max_c"] == 85 and len(b["baseline_sha256"]) == 64 and b["synthetic"] is True
    assert "NOT product safety certification" in b["approval_scope"]
    assert flow.baseline()["baseline_id"] == b["baseline_id"]                      # idempotent lock


def test_unreleased_prd_and_unapproved_design_source_are_refused(flow):
    flow.sources()
    flow.rfq_to_n2()
    flow.prd(release=False)
    assert flow.baseline(expect=409)["detail"]["code"] == "PRD_NOT_RELEASED"
    flow.c.post("/v1/n2/prds/PSU-120W/revisions/A/release", "pa", json={"reason": "PRD approved for design"})
    flow.c.source("SRC-PENDING", permitted_use=("DESIGN_ENGINEERING",), approve=False)
    r = flow.baseline(expect=409, design_source_id="SRC-PENDING")
    assert r["detail"]["code"] == "DESIGN_SOURCE_NOT_APPROVED"


def test_tampered_prd_payload_bytes_break_the_lock(flow):
    flow.sources()
    flow.rfq_to_n2()
    rec = flow.prd()
    store_root = Path(flow.c.app.state.settings.storage_root)
    blob = store_root / "quarantine" / "acme" / rec["release"]["payload_sha256"]
    blob.chmod(0o644)
    blob.write_bytes(blob.read_bytes().replace(b"52.8", b"99.9"))
    r = flow.baseline(expect=409)
    assert r["detail"]["code"] == "PRD_SIGNATURE_INVALID" and "do not match" in r["detail"]["message"]


def test_planning_creates_immutable_hashed_packages_per_scope(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"])
    assert set(wps) == {"ELECTRICAL_SCHEMATIC", "PCB_LAYOUT", "DFM", "CIRCUIT_SIMULATION", "MECHANICAL", "THERMAL"}
    pcb = wps["PCB_LAYOUT"]
    assert pcb["required_native_roles"] == ["PCB", "SCHEMATIC"] and len(pcb["package_sha256"]) == 64
    assert any("BOM refdes reconcile" in c for c in pcb["acceptance_criteria"])
    again = flow.plan(b["baseline_id"])
    assert {k: v["work_package_id"] for k, v in again.items()} == {k: v["work_package_id"] for k, v in wps.items()}
    assert flow.c.c.put(f"/v1/n3/work-packages/{pcb['work_package_id']}", json={}).status_code == 405


def test_work_package_outside_the_prd_scopes_is_refused(flow):
    b = flow.ready(scopes=["circuit_simulation"])
    r = flow.c.post("/v1/n3/work-packages", "de", json={"baseline_id": b["baseline_id"], "kind": "PCB_LAYOUT",
                                                        "budget_cap": {"amount": "100", "currency": "USD"}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "SCOPE_NOT_IN_PRD"


def test_tool_routing_is_bounded_by_capability_and_budget(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], amount="1000")
    mech = flow.route_tool(wps["MECHANICAL"]["work_package_id"], "kicad-cli")
    assert mech.status_code == 409 and any(x.startswith("TOOL_CANNOT_EXECUTE") for x in mech.json()["reasons"])
    over = flow.route_tool(wps["PCB_LAYOUT"]["work_package_id"], "kicad-cli", amount="5000")
    assert over.status_code == 409 and any("BUDGET_EXCEEDS_PACKAGE_CAP" in x for x in over.json()["reasons"])
    ok = flow.route_tool(wps["CIRCUIT_SIMULATION"]["work_package_id"], "ngspice")
    assert ok.status_code == 201 and ok.json()["authorized"]


def test_vendor_routing_needs_onboarding_nda_and_an_n0_transmission_approval(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["PCB_LAYOUT"]["work_package_id"]
    assert flow.route_vendor(wp).status_code == 404                              # not onboarded
    assert flow.onboard(who="de").status_code == 403                             # authority only
    assert flow.onboard("VEN-NONDA", nda_on_file=False).status_code == 201
    assert any(x.startswith("NDA_MISSING") for x in flow.route_vendor(wp, "VEN-NONDA").json()["reasons"])
    assert flow.onboard().status_code == 201
    r = flow.route_vendor(wp)
    assert r.status_code == 201, r.text
    with flow.c.app.state.db.session() as s:                                  # N0 VendorWorkPackage.v1
        row = Repository(s).get_work_package("acme", r.json()["vendor_work_package_id"])
        assert row.status == "AUTHORIZED" and row.record["approved_input_ids"] == ["PRD-PSU-120W@A"]


def test_vendor_not_allowed_by_the_prd_source_is_refused_by_n0(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["PCB_LAYOUT"]["work_package_id"]
    assert flow.onboard("VEN-OTHER").status_code == 201
    r = flow.route_vendor(wp, "VEN-OTHER")
    assert r.status_code == 409 and any("VENDOR_TRANSMISSION_UNAPPROVED" in x for x in r.json()["reasons"])


def test_delivery_manifest_is_verified_file_by_file(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["ELECTRICAL_SCHEMATIC"]["work_package_id"]
    files = {k: v for k, v in design_files().items() if k in ("psu.kicad_sch", "psu.kicad_pro")}
    m = manifest(files)
    m["entries"][0]["sha256"] = "0" * 64
    bad = flow.deliver(wp, files, manifest_body=m)
    assert bad.status_code == 422 and "SHA256_MISMATCH" in bad.json()["rejection_codes"]
    extra = flow.deliver(wp, {**files, "notes.txt": b"hello"}, manifest_body=manifest(files))
    assert "UNDECLARED_FILE" in extra.json()["rejection_codes"]
    missing = flow.deliver(wp, {"psu.kicad_sch": files["psu.kicad_sch"]}, manifest_body=manifest(files))
    assert "MISSING_FILE" in missing.json()["rejection_codes"]
    wrong_rev = flow.deliver(wp, files, revision="1.2")
    assert "REVISION_FORMAT" in wrong_rev.json()["rejection_codes"]
    ok = flow.deliver(wp, files)
    assert ok.status_code == 201 and {f["role"] for f in ok.json()["files"]} == {"SCHEMATIC", "PROJECT"}
    assert all(f["uri"].startswith("n0://quarantine/acme/") for f in ok.json()["files"])


@pytest.mark.parametrize("names", [["psu_datasheet.pdf"], ["psu_datasheet.pdf", "layout_screenshot.png"]])
def test_pdf_or_screenshot_only_deliveries_are_rejected(flow, names):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["PCB_LAYOUT"]["work_package_id"]
    f = design_files()
    r = flow.deliver(wp, {n: f[n] for n in names})
    assert r.status_code == 422 and r.json()["rejection_codes"] == ["NATIVE_FILES_MISSING"]
    assert "PDF / screenshot-only" in r.json()["problems"][0]


def test_fake_native_files_and_malware_are_rejected_before_storage(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["PCB_LAYOUT"]["work_package_id"]
    fake = flow.deliver(wp, {"psu.kicad_pcb": b"%PDF-1.4 exported board"})
    assert fake.status_code == 422 and "UPLOAD_REJECTED" in fake.json()["rejection_codes"]
    f = design_files()
    mal = flow.deliver(wp, {"psu.kicad_pcb": f["psu.kicad_pcb"], "psu_bom.csv": EICAR_BOM})
    assert "MALWARE_DETECTED" in mal.json()["rejection_codes"] and mal.json()["files"] == []
    store = Path(flow.c.app.state.settings.storage_root) / "quarantine" / "acme"
    assert not (store / sha(EICAR_BOM)).exists() and not (store / sha(f["psu.kicad_pcb"])).exists()


def test_tool_jobs_need_a_route_and_current_package_inputs(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["ELECTRICAL_SCHEMATIC"]["work_package_id"]
    sch = design_files()["psu.kicad_sch"]
    assert flow.deliver(wp, {"psu.kicad_sch": sch}).status_code == 201
    r = flow.job(wp, "kicad-cli", "sch_erc", [sha(sch)])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_ROUTED"
    assert flow.route_tool(wp, "kicad-cli").status_code == 201
    r = flow.job(wp, "kicad-cli", "sch_erc", ["f" * 64])
    assert r.status_code == 404 and r.json()["detail"]["code"] == "INPUT_NOT_IN_PACKAGE"
    r = flow.job(wp, "ngspice", "spice_corners", [sha(sch)])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TOOL_NOT_APPLICABLE"
    job = flow.job(wp, "kicad-cli", "sch_erc", [sha(sch)])
    assert job.status_code == 201 and job.json()["status"] == "SUCCEEDED" and job.json()["tool_version"] == "8.0.4"
    assert job.json()["outputs"][0]["role"] == "ERC_REPORT" and job.json()["outputs"][0]["origin"] == "TOOL_JOB"


def test_passing_assessment_records_verified_metrics(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], ["schematic_pcb"])
    a = flow.work_package(wps["PCB_LAYOUT"])
    assert a["decision"] == "PASS" and a["design_revision"] == "B" and len(a["assessment_sha256"]) == 64
    m = a["metrics"]
    assert m["drc"]["critical"] == 0 and m["drc"]["tool_version"] == "8.0.4"
    assert m["pcb"] == {"revision": "B", "refdes": 11, "copper_layers": 2}
    assert m["clearance"] == [{"kind": "clearance", "required_mm": 0.2, "actual_mm": 0.2}]
    assert {i["mpn"] for i in m["bom"]["items"]} >= {"BSC093N15NS5ATMA1", "LM5146RGYR"}


def test_rework_proposals_never_change_the_decision_and_cannot_waive_blocks(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], ["schematic_pcb"])
    f = design_files(**{"psu.kicad_sch": gen.schematic(marker="ATHENA_TEST_ERC pin_not_connected:error")})
    a = flow.work_package(wps["ELECTRICAL_SCHEMATIC"], f)
    assert a["decision"] == "BLOCK" and "CRITICAL_ERC" in codes(a)
    body = {"assessment_id": a["assessment_id"], "currency": "USD", "budget_cap": "1000", "candidates": [
        {"action_id": "WAIVE-ERC", "action_type": "WAIVER_REQUEST", "addresses": ["CRITICAL_ERC"], "cost": "0",
         "risk_reduction_value": "1000000"},
        {"action_id": "FIX-SCH", "action_type": "FIX_IN_HOUSE", "addresses": ["CRITICAL_ERC"], "cost": "400",
         "risk_reduction_value": "5000"},
        {"action_id": "SWAP-U1", "action_type": "COMPONENT_SWAP", "addresses": ["DERATING_BREACH"], "cost": "10",
         "risk_reduction_value": "99"}]}
    r = flow.c.post(f"/v1/n3/assessments/{a['assessment_id']}/actions", "de", json=body)
    assert r.status_code == 201, r.text
    act = r.json()
    assert act["assessment_decision"] == "BLOCK" and act["decision_unchanged"] is True
    assert [x["action_id"] for x in act["ranked"]] == ["FIX-SCH"] and act["ranked"][0]["status"] == "PROPOSED_NOT_EXECUTED"
    assert {x["action_id"]: x["codes"] for x in act["blocked"]} == {"WAIVE-ERC": ["BLOCK_NOT_WAIVABLE"],
                                                                     "SWAP-U1": ["NOT_RELEVANT"]}
    assert flow.c.get(f"/v1/n3/assessments/{a['assessment_id']}", "de").json()["decision"] == "BLOCK"


def test_design_authority_cannot_approve_block_or_rework(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], ["schematic_pcb"])
    blocked = flow.work_package(wps["ELECTRICAL_SCHEMATIC"], design_files(**{
        "psu.kicad_sch": gen.schematic(marker="ATHENA_TEST_ERC pin_not_connected:error")}))
    r = flow.review(blocked["assessment_id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BLOCK_NOT_APPROVABLE"
    rework = flow.work_package(wps["DFM"], run_jobs=False)                     # no Gerber / drill outputs yet
    assert rework["decision"] == "REWORK" and "MANUFACTURING_OUTPUTS_MISSING" in codes(rework)
    r = flow.review(rework["assessment_id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "REWORK_OPEN"
    assert flow.review(rework["assessment_id"], decision="REJECTED").status_code == 201


def test_release_requires_every_package_of_the_scope_to_be_approved(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], ["schematic_pcb"])
    a = flow.work_package(wps["ELECTRICAL_SCHEMATIC"])
    assert flow.review(a["assessment_id"]).status_code == 201
    r = flow.release(b["baseline_id"], "schematic_pcb")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PACKAGE_NOT_APPROVED"
    assert "PCB_LAYOUT: not assessed" in r.json()["detail"]["message"]
    assert flow.release(b["baseline_id"], "schematic_pcb", who="de").status_code == 403


def test_release_payloads_are_n0_checked_and_locked_to_the_prd(flow):
    b = flow.ready()
    flow.scope(b["baseline_id"], "schematic_pcb")
    r = flow.release(b["baseline_id"], "schematic_pcb")
    assert r.status_code == 201 and r.json()["status"] == "RELEASED", r.text
    dsn = flow.c.get("/v1/artifacts/DSN-PSU-120W-schematic_pcb", "aud").json()
    art = dsn["canonical_artifact"]
    assert art["source_artifact_ids"] == ["PRD-PSU-120W@A"] and art["revision"] == "B"
    assert {Path(x["uri"]).name for x in art["file_refs"]} == {sha(design_files()[n]) for n in
                                                               ("psu.kicad_sch", "psu.kicad_pcb", "psu.kicad_pro")}
    assert all(x["uri"].startswith("n0://released/") for x in art["file_refs"])      # promoted on PASS
    p = art["payload"]
    assert p["prd_payload_sha256"] == b["prd_payload_sha256"] and p["erc"]["critical"] == p["drc"]["critical"] == 0
    assert p["test_limits"][0]["parameter"] == "vout_min" and "NOT product safety" in p["approval_scope"]
    bom = flow.c.get("/v1/artifacts/BOM-PSU-120W-schematic_pcb", "aud").json()["canonical_artifact"]["payload"]
    assert bom["design_revision"] == bom["pcb_revision"] == "B"
    assert all(i["schema_version"] == "bom_item.v1" for i in bom["items"])
    assert {r for i in bom["items"] for r in i["refdes"]} == {p_[0] for p_ in gen.PARTS}


def test_feedback_on_synthetic_designs_is_never_training_eligible(flow):
    b = flow.ready()
    a = flow.work_package(flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"])
    r = flow.c.post("/v1/n3/feedback", "da", json={"target_kind": "ASSESSMENT", "target_id": a["assessment_id"],
                                                   "label": "CORRECT", "reason": "matches bench data"})
    assert r.status_code == 201 and r.json()["training_eligible"] is False
    r = flow.c.post("/v1/n3/feedback", "de", json={"target_kind": "FINDING", "target_id": "N3F-nope",
                                                   "label": "FALSE_POSITIVE", "reason": "unknown finding"})
    assert r.status_code == 404


def test_status_views_track_each_package(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"]
    wid = wp["work_package_id"]
    assert flow.c.get(f"/v1/n3/subjects/work_package/{wid}/status", "de").json()["status"] == "PLANNED"
    a = flow.work_package(wp)
    flow.review(a["assessment_id"])
    st = flow.c.get(f"/v1/n3/subjects/work_package/{wid}/status", "aud").json()
    assert [h["status"] for h in st["history"]] == ["PLANNED", "ROUTED", "DELIVERED", "ASSESSED", "REVIEW_APPROVED"]
    job = flow.c.get(f"/v1/n3/work-packages/{wid}", "de").json()["jobs"][0]
    js = flow.c.get(f"/v1/n3/subjects/job/{job['job_id']}/status", "de").json()
    assert [h["status"] for h in js["history"]] == ["QUEUED", "RUNNING", "SUCCEEDED"]
    st = flow.c.get("/v1/n3/status", "de").json()
    assert st["tools"]["ngspice"]["version"] == "42" and st["sandbox"]["shell"] is False
    assert json.loads(json.dumps(st))["operations"]["spice_corners"] == "ngspice"
