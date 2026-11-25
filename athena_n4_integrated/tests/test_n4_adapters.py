"""N4.4 intake adapters: live SCPI over TCP (a local SCPI server stands in for the bench instrument),
SCPI log identity / attachment checks, accredited lab reports, AOI triage and human-only training
labels, and the offline CSV importer."""
from __future__ import annotations

import pytest

from app.n4.adapters import scpi
from n4_support import CAL, FakeScpiServer, codes, days, iso


@pytest.fixture
def planned(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"])
    flow4.pfmea(p["plan_id"])
    return flow4, p["plan_id"]


@pytest.fixture
def bench_server():
    srv = FakeScpiServer(idn="Keysight Technologies,34465A,MY60000001,A.03.02-02.40",
                         readings={"MEAS:VOLT:DC?": "+2.40012000E+01", "MEAS:CURR:DC?": "+9.9E37"})
    yield srv
    srv.close()


def _live_instrument(flow, port: int, host: str = "127.0.0.1"):
    body = {"instrument_id": "DMM-LIVE", "tenant_id": "acme", "kind": "DMM", "manufacturer": "Keysight Technologies",
            "model": "34465A", "serial": "MY60000001", "firmware": "A.03.02-02.40",
            "resource": f"TCPIP0::{host}::{port}::SOCKET"}
    assert flow.c.post("/v1/n4/instruments", "te", json=body).status_code == 201
    flow.calibrate("DMM-LIVE", "CAL-DMM-LIVE")


def _capture(flow, pid, steps, **kw):
    return flow.c.post(f"/v1/n4/plans/{pid}/evidence/scpi/live", "te", json={
        "test_id": "T-01", "instrument_id": "DMM-LIVE", "unit_serial": "PSU-0003", "source_id": "SRC-LAB",
        "station_id": "BENCH-2", "method_ref": {"method_id": "SOP-ATE-001", "revision": "C"}, "steps": steps, **kw})


def test_live_scpi_capture_records_identity_firmware_calibration_and_utc(planned, bench_server):
    flow, pid = planned
    _live_instrument(flow, bench_server.port)
    r = _capture(flow, pid, [{"query": "MEAS:VOLT:DC?", "characteristic": "output_voltage", "unit": "V",
                              "expanded_uncertainty": "0.012"}])
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["adapter"] == "SCPI_LIVE" and ev["derived_provenance"] == "PHYSICAL_BENCH"
    assert ev["metadata"]["idn"] == {"manufacturer": "Keysight Technologies", "model": "34465A",
                                     "serial": "MY60000001", "firmware": "A.03.02-02.40"}
    assert bench_server.seen == ["*IDN?", "MEAS:VOLT:DC?", "SYST:ERR?"]               # read-only queries only
    view = flow.c.get(f"/v1/n4/plans/{pid}", "aud").json()
    assert view["measurements"] >= 1 and ev["files"][0]["role"] == "RAW_LOG"
    a = flow.n4_assess(pid)
    m = next(x for v in a["test_verdicts"] if v["test_id"] == "T-01" for x in v["measurements"]
             if x["unit_serial"] == "PSU-0003")
    assert m["value"] == "24.0012" and m["calibration_id"] == "CAL-DMM-LIVE" and m["captured_at"].endswith("Z")


def test_live_capture_refuses_write_commands_and_unlisted_hosts(planned, bench_server):
    flow, pid = planned
    _live_instrument(flow, bench_server.port)
    r = _capture(flow, pid, [{"query": "*RST", "characteristic": "output_voltage", "unit": "V",
                              "expanded_uncertainty": "0.01"}])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "COMMAND_NOT_ALLOWED"
    r = _capture(flow, pid, [{"query": "SOUR:VOLT 48;OUTP ON", "characteristic": "output_voltage", "unit": "V",
                              "expanded_uncertainty": "0.01"}])
    assert r.status_code == 403
    assert bench_server.seen == []                                                     # nothing was sent


def test_live_capture_only_reaches_allowlisted_hosts(planned):
    flow, pid = planned
    body = {"instrument_id": "DMM-LIVE", "tenant_id": "acme", "kind": "DMM", "manufacturer": "Keysight Technologies",
            "model": "34465A", "serial": "MY60000001", "resource": "TCPIP0::192.0.2.10::5025::SOCKET"}
    flow.c.post("/v1/n4/instruments", "te", json=body)
    r = _capture(flow, pid, [{"query": "MEAS:VOLT:DC?", "characteristic": "output_voltage", "unit": "V",
                              "expanded_uncertainty": "0.01"}])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "HOST_NOT_ALLOWED"


def test_instrument_overload_is_never_a_reading(planned, bench_server):
    flow, pid = planned
    _live_instrument(flow, bench_server.port)
    r = _capture(flow, pid, [{"query": "MEAS:CURR:DC?", "characteristic": "output_voltage", "unit": "V",
                              "expanded_uncertainty": "0.01"}])
    assert r.status_code == 422 and "INSTRUMENT_OVERLOAD" in r.json()["rejection_codes"]


def test_scpi_helpers():
    assert scpi.parse_idn("Rohde&Schwarz,RTB2004,102233,02.300")["serial"] == "102233"
    assert scpi.looks_simulated(scpi.parse_idn("Simulated Instruments,DMM-1,SIM001,0"))
    assert not scpi.looks_simulated(scpi.parse_idn("Keysight Technologies,34465A,MY1,A"))
    assert scpi.parse_number("+2.40012E+01 V") == scpi.parse_number("24.0012")
    with pytest.raises(scpi.ScpiError):
        scpi.parse_idn("garbage")
    assert scpi.parse_resource("TCPIP0::10.1.2.3::5025::SOCKET") == ("socket", "10.1.2.3", 5025)
    assert scpi.QUERY_ALLOWLIST.match("MEAS:VOLT:DC? 10,0.001") and not scpi.QUERY_ALLOWLIST.match("*RST")


def test_scpi_log_attachments_are_hash_checked_and_timestamps_must_be_utc(planned):
    flow, pid = planned
    wf = {"ripple.csv": b"t,v\n0,0.01\n"}
    name, data = flow.scpi_log("T-07", [("MEAS:VOLT:PTP?", "48", "ripple_voltage", "mV", "4")], attachments=wf)
    r = flow.post_scpi(pid, name, data, attachments={"ripple.csv": b"t,v\n0,0.99\n"})
    assert r.status_code == 422 and "SHA256_MISMATCH" in r.json()["rejection_codes"]
    r = flow.post_scpi(pid, name, data)
    assert "MISSING_FILE" in r.json()["rejection_codes"]
    ok = flow.post_scpi(pid, name, data, attachments=wf)
    assert ok.status_code == 201 and {f["role"] for f in ok.json()["files"]} == {"RAW_LOG", "WAVEFORM"}
    local = data.replace(b'Z"', b'+02:00"')
    assert "TIMESTAMP_NOT_UTC" in flow.post_scpi(pid, name, local).json()["rejection_codes"]


def test_lab_must_be_accredited_in_scope_and_cite_its_certificate(planned):
    flow, pid = planned
    pdf = flow.lab_pdf()
    res = [flow.lab_result("T-09", "hipot_leakage_current", "1.2", "mA", "0.1")]
    r = flow.post_lab(pid, pdf, flow.lab_manifest(pdf, res, accreditation="D-PL-00000-00-00"))
    assert r.status_code == 422 and "LAB_NOT_ACCREDITED" in r.json()["rejection_codes"]
    out = {**res[0], "standard_clause": "IEC 61010-1 cl. 6"}
    assert "LAB_SCOPE_EXCEEDED" in flow.post_lab(pid, pdf, flow.lab_manifest(pdf, [out])).json()["rejection_codes"]
    other = flow.lab_pdf(cert="LAB-OTHER-1")
    r = flow.post_lab(pid, other, flow.lab_manifest(other, res, cert="LAB-2026-0042"))
    assert "CERTIFICATE_NOT_IN_REPORT" in r.json()["rejection_codes"]
    m = flow.lab_manifest(pdf, res)
    m["report_sha256"] = "0" * 64
    assert "SHA256_MISMATCH" in flow.post_lab(pid, pdf, m).json()["rejection_codes"]
    ok = flow.post_lab(pid, pdf, flow.lab_manifest(pdf, res))
    assert ok.status_code == 201 and ok.json()["derived_provenance"] == "ACCREDITED_LAB"
    assert ok.json()["metadata"]["certificate_number"] == "LAB-2026-0042"


def test_lab_results_must_target_lab_tests_and_calibration_must_be_in_date(planned):
    flow, pid = planned
    pdf = flow.lab_pdf()
    r = flow.post_lab(pid, pdf, flow.lab_manifest(pdf, [flow.lab_result("T-01", "output_voltage", "24", "V", "0.05")]))
    assert "METHOD_ADAPTER_MISMATCH" in r.json()["rejection_codes"]
    late = flow.lab_result("T-10", "insulation_resistance", "850", "Mohm", "40", due=days(-5), at=days(-2))
    assert flow.post_lab(pid, pdf, flow.lab_manifest(pdf, [late])).status_code == 201
    a = flow.n4_assess(pid)
    assert any(f["code"] == "CALIBRATION_EXPIRED" and f["test_id"] == "T-10" for f in a["findings"])


def test_aoi_triage_thresholds_and_pcb_revision(planned):
    flow, pid = planned
    dets = [{"class": "bridge", "confidence": c, "bbox_xyxy": [0, 0, 1, 1]} for c in (0.95, 0.85, 0.6, 0.4, 0.39)]
    r = flow.aoi(pid, dets)
    assert [d["triage"] for d in r.json()["detections"]] == ["MODEL_FLAGGED", "MODEL_FLAGGED", "AMBIGUOUS",
                                                             "AMBIGUOUS", "BELOW_THRESHOLD"]
    assert r.json()["metadata"]["vision_release_decisions"] == "DEFERRED_GATED"
    bad = flow.aoi(pid, [], pcb="C")
    assert bad.status_code == 422 and "WRONG_PCB_REVISION" in bad.json()["rejection_codes"]


def test_only_double_labeled_human_adjudications_are_training_eligible(planned):
    flow, pid = planned
    r = flow.aoi(pid, [{"class": "bridge", "confidence": 0.9, "bbox_xyxy": [0, 0, 1, 1]},
                       {"class": "void", "confidence": 0.5, "bbox_xyxy": [2, 2, 3, 3]},
                       {"class": "skew", "confidence": 0.6, "bbox_xyxy": [4, 4, 5, 5]}])
    ev, d = r.json()["evidence_id"], [x["detection_id"] for x in r.json()["detections"]]
    flow.adjudicate(ev, [{"detection_id": d[0], "label": "CONFIRMED_DEFECT"},
                         {"detection_id": d[1], "label": "FALSE_CALL"}], verdict="REJECT")
    flow.adjudicate(ev, [{"detection_id": d[0], "label": "CONFIRMED_DEFECT"},
                         {"detection_id": d[1], "label": "CONFIRMED_DEFECT"}], verdict="REJECT", who="qr2")
    t = flow.c.get("/v1/n4/aoi/training-labels", "qr").json()
    by = {x["detection_id"]: x for x in t["labels"]}
    assert by[d[0]]["double_labeled"] and by[d[0]]["training_eligible"] and by[d[0]]["label"] == "CONFIRMED_DEFECT"
    assert by[d[1]]["label"] == "DISAGREEMENT" and not by[d[1]]["training_eligible"]
    assert d[2] not in by and t["model_only_labels_excluded"] == 1                   # never adjudicated


def test_csv_importer_checks_columns_instruments_and_methods(planned):
    flow, pid = planned
    r = flow.c.post(f"/v1/n4/plans/{pid}/evidence/csv", "li", data={"source_id": "SRC-LAB"},
                    files=[("files", ("x.csv", b"test_id,value\nT-12,7\n", "text/csv"))])
    assert r.status_code == 422 and "CSV_COLUMNS_MISSING" in r.json()["rejection_codes"]
    assert "INSTRUMENT_UNREGISTERED" in flow.csv(pid, [flow.creepage_row(instrument_id="NOPE")]).json()[
        "rejection_codes"]
    assert "CALIBRATION_MISMATCH" in flow.csv(pid, [flow.creepage_row(calibration_id=CAL["DMM-1"])]).json()[
        "rejection_codes"]
    lab_row = {**flow.creepage_row(), "test_id": "T-09"}
    assert "METHOD_ADAPTER_MISMATCH" in flow.csv(pid, [lab_row]).json()["rejection_codes"]
    tz = flow.creepage_row(captured_at=iso(days(-1)).replace("Z", ""))
    assert "TIMESTAMP_NOT_UTC" in flow.csv(pid, [tz]).json()["rejection_codes"]
    ok = flow.csv(pid, [flow.creepage_row()])
    assert ok.status_code == 201 and ok.json()["derived_provenance"] == "PHYSICAL_INSPECTION"
    a = flow.n4_assess(pid)
    t12 = next(v for v in a["test_verdicts"] if v["test_id"] == "T-12")
    assert t12["verdict"] == "PASS" and t12["measurements"][0]["value"] == "7.1"
    assert "NOT_TESTED_SAFETY" in codes(a)                                         # other safety tests have no data
