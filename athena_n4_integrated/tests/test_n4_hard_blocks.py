"""Invariants 1, 2 and 4: guard-band INDETERMINATE, FAIL, calibration, SOP, traceability, PCB revision,
evidence hashes and synthetic-as-physical all hard-BLOCK, and nothing can approve past a BLOCK. Each
case starts from a fully passing plan and changes exactly one thing."""
from __future__ import annotations

from pathlib import Path

import pytest

from n4_support import codes, days, plan_cases


@pytest.fixture
def ok(flow4):
    i, p, a = flow4.verified()
    return flow4, p["plan_id"], a


def _reassess(flow, plan_id):
    return flow.n4_assess(plan_id)


def test_guard_band_crossing_the_limit_is_indeterminate_and_blocks(ok):
    flow, pid, _ = ok
    r = flow.bench(pid, "T-01", value="24.48", u="0.05", serial="PSU-0002")        # 24.53 > 24.5 V max
    assert r.status_code == 201
    a = _reassess(flow, pid)
    t01 = next(v for v in a["test_verdicts"] if v["test_id"] == "T-01")
    assert a["decision"] == "BLOCK" and t01["verdict"] == "INDETERMINATE"
    f = next(x for x in a["findings"] if x["code"] == "GUARD_BAND_INDETERMINATE")
    assert "24.48 ± 0.05" in f["message"] and "cannot be accepted" in f["message"]
    m = next(x for x in t01["measurements"] if x["unit_serial"] == "PSU-0002")
    assert m["verdict"] == "INDETERMINATE"
    r = flow.n4_review(a, reason="the reading is only 0.02 V below the limit, accept it")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BLOCK_NOT_APPROVABLE"


def test_reading_on_the_guard_band_edge_passes(ok):
    flow, pid, _ = ok
    flow.bench(pid, "T-01", value="24.45", u="0.05", serial="PSU-0002")             # 24.50 ≤ 24.5 V
    assert _reassess(flow, pid)["decision"] == "PASS"


def test_a_failing_measurement_blocks(ok):
    flow, pid, _ = ok
    flow.bench(pid, "T-03", value="24.71", serial="PSU-0002")
    a = _reassess(flow, pid)
    assert a["decision"] == "BLOCK" and "MEASUREMENT_FAIL" in codes(a, "BLOCK")


def test_expired_calibration_blocks(ok):
    flow, pid, _ = ok
    flow.calibrate("DMM-1", "CAL-DMM-OLD", at=days(-400), due=days(-10))
    flow.bench(pid, "T-02", cal="CAL-DMM-OLD", serial="PSU-0002")
    a = _reassess(flow, pid)
    f = next(x for x in a["findings"] if x["code"] == "CALIBRATION_EXPIRED")
    assert a["decision"] == "BLOCK" and f["test_id"] == "T-02" and "was due" in f["message"]


def test_revoked_calibration_blocks_every_measurement_made_with_it(ok):
    flow, pid, _ = ok
    assert flow.c.post("/v1/n4/calibrations/CAL-DMM-1/revoke", "qr", json={"reason": "drift found"}).status_code == 200
    a = _reassess(flow, pid)
    hit = {x["test_id"] for x in a["findings"] if x["code"] == "CALIBRATION_REVOKED"}
    assert a["decision"] == "BLOCK" and hit == {"T-01", "T-02", "T-03", "T-04", "T-05", "T-06", "T-07"}


@pytest.mark.parametrize("manifest_rev, pdf_rev", [("C", "C"), ("B", "C"), ("B", None)])
def test_lab_report_for_another_pcb_revision_is_rejected_and_blocks(ok, manifest_rev, pdf_rev):
    flow, pid, _ = ok
    pdf = flow.lab_pdf(cert="LAB-2026-0099", pcb=pdf_rev) if pdf_rev else \
        flow.lab_pdf(cert="LAB-2026-0099").replace(b"PCB Rev B", b"Board: PSU")
    m = flow.lab_manifest(pdf, [flow.lab_result("T-09", "hipot_leakage_current", "1.1", "mA", "0.1")],
                          pcb=manifest_rev, cert="LAB-2026-0099")
    r = flow.post_lab(pid, pdf, m)
    assert r.status_code == 422 and "WRONG_PCB_REVISION" in r.json()["rejection_codes"]
    assert r.json()["files"][0]["blob_id"] == "not-stored"
    a = _reassess(flow, pid)
    assert a["decision"] == "BLOCK" and "WRONG_PCB_REVISION" in codes(a, "BLOCK")
    good = flow.lab_pdf(cert="LAB-2026-0100")
    fixed = flow.post_lab(pid, good, flow.lab_manifest(
        good, [flow.lab_result("T-09", "hipot_leakage_current", "1.1", "mA", "0.1")], cert="LAB-2026-0100"))
    assert fixed.status_code == 201
    assert _reassess(flow, pid)["decision"] == "PASS"                          # superseded by a correct report


def test_unmapped_critical_hazard_blocks(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"], [c for c in plan_cases() if c["test_id"] != "T-11"])     # H-2 touch current
    flow4.pfmea(p["plan_id"])
    a = flow4.n4_assess(p["plan_id"])
    msgs = [x["message"] for x in a["findings"] if x["code"] == "UNMAPPED_SAFETY_CRITICAL"]
    assert a["decision"] == "BLOCK" and any("R-010" in m and "H-2" in m for m in msgs)
    assert any(m.startswith("hazard H-2") for m in msgs)


def test_safety_requirement_verified_only_by_simulation_blocks(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    cases = [c for c in plan_cases() if c["test_id"] != "T-08"] + [
        {**plan_cases()[13], "test_id": "T-15", "requirement_ids": ["R-006", "R-007"]}]
    p = flow4.n4_plan(i["intake_id"], cases)
    a = flow4.n4_assess(p["plan_id"])
    assert "SAFETY_REQUIRES_PHYSICAL" in codes(a, "BLOCK")


@pytest.mark.parametrize("source, idn, claimed", [
    ("SRC-SIMDATA", None, "PHYSICAL"),                                              # synthetic source
    ("SRC-LAB", "pyvisa-sim,34465A,MY59012345,1.0", "PHYSICAL"),                    # simulated instrument
])
def test_synthetic_as_physical_is_rejected_and_blocks(ok, source, idn, claimed):
    flow, pid, _ = ok
    name, data = flow.scpi_log("T-05", [("MEAS:POW:AC?", "121.0", "output_power", "W", "0.6")], serial="PSU-0002",
                               idn=idn, claimed=claimed)
    r = flow.post_scpi(pid, name, data, source=source)
    assert r.status_code == 422 and "SYNTHETIC_AS_PHYSICAL" in r.json()["rejection_codes"]
    a = _reassess(flow, pid)
    assert a["decision"] == "BLOCK" and "SYNTHETIC_NOT_QUALIFICATION" in codes(a, "BLOCK")


def test_honestly_synthetic_or_mock_evidence_never_qualifies(ok):
    flow, pid, _ = ok
    name, data = flow.scpi_log("T-06", [("MEAS:POW:EFF?", "93.0", "efficiency", "%", "0.3")], serial="PSU-0002",
                               claimed="SYNTHETIC")
    r = flow.post_scpi(pid, name, data, source="SRC-SIMDATA")
    assert r.status_code == 201 and r.json()["derived_provenance"] == "SYNTHETIC"
    mock = flow.csv(pid, [flow.creepage_row(serial="PSU-0002")], mock=True)
    assert mock.status_code == 201 and mock.json()["derived_provenance"] == "MOCK_FIXTURE", mock.text
    a = _reassess(flow, pid)
    hit = {x["test_id"] for x in a["findings"] if x["code"] == "SYNTHETIC_NOT_QUALIFICATION"}
    assert a["decision"] == "BLOCK" and hit == {"T-06", "T-12"}


@pytest.mark.parametrize("change, code", [
    (lambda c: c.update(method_ref=None), "METHOD_SOP_MISSING"),
    (lambda c: c.update(method_ref={"method_id": "SOP-ATE-001", "revision": "D"}), "METHOD_SOP_MISSING"),
])
def test_missing_or_unapproved_sop_revision_blocks(flow4, change, code):
    flow4.designed()
    flow4.registries()
    flow4.method("SOP-ATE-001", "D", "BENCH_ATE", approve=False)                   # drafted, never approved
    i = flow4.intake()
    cases = plan_cases()
    change(cases[0])
    p = flow4.n4_plan(i["intake_id"], cases)
    a = flow4.n4_assess(p["plan_id"])
    assert a["decision"] == "BLOCK" and any(f["code"] == code and f["test_id"] == "T-01" for f in a["findings"])


def test_measurement_made_under_another_sop_revision_blocks(ok):
    flow, pid, _ = ok
    flow.method("SOP-ATE-001", "B", "BENCH_ATE")
    flow.bench(pid, "T-04", serial="PSU-0002", method=("SOP-ATE-001", "B"))
    a = _reassess(flow, pid)
    assert "METHOD_REVISION_MISMATCH" in codes(a, "BLOCK")


def test_tampered_stored_evidence_blocks(ok):
    flow, pid, a0 = ok
    ev = next(e for e in flow.c.get(f"/v1/n4/plans/{pid}", "aud").json()["evidence"] if e["adapter"] == "SCPI_VISA")
    path = Path(flow.c.app.state.settings.storage_root, "quarantine", "acme", ev["files"][0]["sha256"])
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b"24.00", b"24.10"))
    a = _reassess(flow, pid)
    assert a["decision"] == "BLOCK" and "EVIDENCE_HASH_MISMATCH" in codes(a, "BLOCK")


@pytest.mark.parametrize("idn, code", [
    ("Keysight Technologies,34470A,MY59012345,A.03.01", "INSTRUMENT_IDENTITY_MISMATCH"),
    ("Keysight Technologies,34465A,MY00000000,A.03.01", "INSTRUMENT_UNREGISTERED"),
])
def test_instrument_identity_must_match_the_register(ok, idn, code):
    flow, pid, _ = ok
    name, data = flow.scpi_log("T-01", [("MEAS:VOLT:DC?", "24.0", "output_voltage", "V", "0.05")], idn=idn,
                               serial="PSU-0002")
    r = flow.post_scpi(pid, name, data)
    assert r.status_code == 422 and code in r.json()["rejection_codes"]


def test_limits_looser_than_the_requirement_block(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    cases = plan_cases()
    cases[0]["acceptance"][0].update(lower="23.0", upper="25.0")                      # R-002 is 24 V ± 0.5 V
    p = flow4.n4_plan(i["intake_id"], cases)
    a = flow4.n4_assess(p["plan_id"])
    f = next(x for x in a["findings"] if x["code"] == "LIMIT_LOOSER_THAN_REQUIREMENT")
    assert f["test_id"] == "T-01" and f["requirement_id"] == "R-002" and "[23.5, 24.5]" in f["message"]


def test_untested_safety_case_blocks_and_untested_other_case_is_rework(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"], [c for c in plan_cases() if c["test_id"] in ("T-07", "T-09")])
    flow4.pfmea(p["plan_id"])
    a = flow4.n4_assess(p["plan_id"])
    by = {(f["code"], f["test_id"]) for f in a["findings"]}
    assert ("NOT_TESTED_SAFETY", "T-09") in by and ("NOT_TESTED", "T-07") in by
    assert {v["verdict"] for v in a["test_verdicts"]} == {"NOT_TESTED"}


def test_aoi_needs_independent_human_adjudication(ok):
    flow, pid, _ = ok
    r = flow.aoi(pid, [{"class": "solder_bridge", "confidence": 0.91, "bbox_xyxy": [1, 1, 5, 5]},
                       {"class": "tombstone", "confidence": 0.5, "bbox_xyxy": [9, 9, 12, 12]}], serial="PSU-0002")
    assert [d["triage"] for d in r.json()["detections"]] == ["MODEL_FLAGGED", "AMBIGUOUS"]
    a = _reassess(flow, pid)
    assert "AOI_UNADJUDICATED" in codes(a, "BLOCK")
    ev, dets = r.json()["evidence_id"], r.json()["detections"]
    assert flow.adjudicate(ev, [], who="li").status_code == 403                       # importer may not adjudicate
    flow.adjudicate(ev, [{"detection_id": dets[0]["detection_id"], "label": "CONFIRMED_DEFECT"},
                         {"detection_id": dets[1]["detection_id"], "label": "FALSE_CALL"}], verdict="REJECT")
    a = _reassess(flow, pid)
    assert "INSPECTION_DEFECT" in codes(a, "BLOCK") and "AOI_UNADJUDICATED" not in codes(a)


def test_a_clean_model_result_alone_never_passes_inspection(ok):
    flow, pid, _ = ok
    r = flow.aoi(pid, [], serial="PSU-0002")                                         # model found nothing
    assert r.status_code == 201 and r.json()["detections"] == []
    a = _reassess(flow, pid)
    assert a["decision"] == "BLOCK" and "AOI_UNADJUDICATED" in codes(a, "BLOCK")
