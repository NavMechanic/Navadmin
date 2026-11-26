"""N0 release policy for N4 verification packages (VERIFICATION_RELEASE → N8 / N6 / N7), PRD hazard
references, the shared guard-band decision rule and Parquet evidence sniffing."""
from __future__ import annotations

import copy
from decimal import Decimal as D

import pytest

from app.rules import canonical_json, guard_band_verdict, sha256_hex
from app.scanning import sniff_mime, validate_upload
from conftest import codes
from test_design_artifacts import _art, _release

SHA = "c" * 64


@pytest.mark.parametrize("value, u, lo, hi, verdict", [
    ("24.00", "0.05", "23.5", "24.5", "PASS"),
    ("24.48", "0.05", "23.5", "24.5", "INDETERMINATE"),        # 24.53 crosses the 24.5 V maximum
    ("24.45", "0.05", "23.5", "24.5", "PASS"),                 # exactly on the guard band
    ("24.56", "0.05", "23.5", "24.5", "FAIL"),
    ("23.52", "0.05", "23.5", "24.5", "INDETERMINATE"),
    ("0.93", "0.004", "0.92", None, "PASS"),                    # one-sided minimum
    ("0.0003", "0.00002", None, "0.00025", "FAIL"),             # one-sided maximum
])
def test_guard_band_verdict(value, u, lo, hi, verdict):
    assert guard_band_verdict(D(value), D(u), D(lo) if lo else None, D(hi) if hi else None) == verdict


@pytest.fixture
def ver(n0):
    n0.source("SRC-ENG", permitted_use=("VERIFICATION", "SOURCING", "PRODUCT_DEFINITION", "LIFECYCLE",
                                        "COMMERCIAL", "FINANCE"))
    dsn = _art("DSN-1", "DESIGN_RELEASE", _release("schematic_pcb"), "VERIFICATION")
    dsn["revision"] = "B"
    assert n0.evaluate(dsn)["decision"]["decision"] == "PASS"
    return n0


def _measure(**over) -> dict:
    m = {"measurement_id": "M-1", "test_id": "T-01", "characteristic": "output_voltage", "unit_serial": "PSU-0001",
         "value": "24.00", "unit": "V", "expanded_uncertainty": "0.05", "coverage_factor": 2, "lower_limit": "23.5",
         "upper_limit": "24.5", "verdict": "PASS", "method": "BENCH_ATE", "provenance": "PHYSICAL_ATE",
         "instrument_id": "DMM-1", "calibration_id": "CAL-1", "calibration_due": "2027-06-01T00:00:00Z",
         "captured_at": "2026-11-20T10:00:00Z", "raw_sha256": SHA}
    m.update(over)
    return m


def _vrel(**over) -> dict:
    p = {"release_id": "N4R-1", "target": "N8", "prd_ref": "PRD-1@A", "design_refs": ["DSN-1@B"],
         "design_revision": "B", "pcb_revision": "B", "plan_id": "N4P-1", "plan_sha256": SHA, "assessment_id": "N4A-1",
         "evidence_sha256": SHA, "review_id": "N4RV-1", "approved_by": "ta", "evidence_submitters": ["lab", "te"],
         "verification_status": "VERIFIED", "measurements": [_measure()],
         "traceability": [{"requirement_id": "R-002", "safety_critical": True, "hazard_ids": ["H-1"],
                           "test_ids": ["T-01"], "methods": ["BENCH_ATE"], "verdict": "VERIFIED"}],
         "unit_serials": ["PSU-0001"], "approval_scope": "next controlled lifecycle step only"}
    p.update(over)
    return p


def _vart(payload: dict, purpose: str = "LIFECYCLE", **over) -> dict:
    a = {**_art("N4V-1", "VERIFICATION_RELEASE", payload, purpose), "source_artifact_ids": ["DSN-1@B"]}
    a.update(over)
    a["source_sha256"] = sha256_hex(canonical_json(a["payload"]))
    return a


@pytest.mark.parametrize("purpose, dest", [("LIFECYCLE", ["N8"]), ("COMMERCIAL", ["N6", "N7"]), ("FINANCE", ["N7"])])
def test_verified_package_passes_and_routes_to_n8_n6_n7(ver, purpose, dest):
    res = ver.evaluate(_vart(_vrel(), purpose))
    assert res["decision"]["decision"] == "PASS", res["decision"]["explanation"]
    assert res["decision"]["allowed_destinations"] == dest


@pytest.mark.parametrize("change, code", [
    ({"measurements": [_measure(value="24.48", verdict="PASS")]}, "GUARD_BAND_INDETERMINATE"),
    ({"measurements": [_measure(value="24.48", verdict="INDETERMINATE")]}, "GUARD_BAND_INDETERMINATE"),
    ({"measurements": [_measure(value="24.60", verdict="FAIL")]}, "MEASUREMENT_FAIL"),
    ({"measurements": [_measure(calibration_due="2026-11-01T00:00:00Z")]}, "CALIBRATION_EXPIRED"),
    ({"measurements": [_measure(calibration_id=None)]}, "CALIBRATION_MISSING"),
    ({"measurements": [_measure(expanded_uncertainty=None)]}, "UNCERTAINTY_MISSING"),
    ({"measurements": [_measure(unit="volts-ish")]}, "UNIT_NOT_SI"),
    ({"measurements": [_measure(provenance="SYNTHETIC")]}, "SYNTHETIC_AS_PHYSICAL"),
    ({"measurements": [_measure(provenance="MOCK_FIXTURE")]}, "SYNTHETIC_AS_PHYSICAL"),
    ({"measurements": [_measure(provenance="SIMULATION")]}, "PROVENANCE_METHOD_MISMATCH"),
    ({"measurements": [_measure(raw_sha256=None)]}, "EVIDENCE_HASH_MISSING"),
    ({"measurements": [_measure(method="SIM", provenance="SIMULATION")]}, "UNMAPPED_SAFETY_CRITICAL"),
    ({"verification_status": "NOT_VERIFIED"}, "VERIFICATION_INCOMPLETE"),
    ({"approved_by": "lab"}, "SEPARATION_OF_DUTIES"),
    ({"prioritization_method": "AIAG_VDA_AP_LICENSED"}, "STANDARD_OVERCLAIM"),
    ({"reliability": {"label": "MTBF", "mtbf_h": 120000}}, "RELIABILITY_OVERCLAIM"),
    ({"commercial": {"claims": ["MTBF 120 000 h"]}}, "RELIABILITY_OVERCLAIM"),
    ({"design_refs": ["DSN-OTHER@B"]}, "DESIGN_REVISION_UNLOCKED"),
    ({"design_revision": "C"}, "DESIGN_REVISION_UNLOCKED"),
])
def test_verification_hard_policies_block(ver, change, code):
    res = ver.evaluate(_vart(_vrel(**change)))
    assert res["decision"]["decision"] == "BLOCK" and code in codes(res), res["decision"]["explanation"]


_INSP = {"test_id": "T-13", "unit_serial": "PSU-0001", "defects_confirmed": 0, "unadjudicated": 0,
         "human_verdict": "ACCEPT", "adjudicator": "qr", "evidence_sha256": SHA}


@pytest.mark.parametrize("change, ok", [({}, True), ({"defects_confirmed": 1}, False), ({"unadjudicated": 2}, False),
                                        ({"human_verdict": "REJECT"}, False), ({"adjudicator": "lab"}, False),
                                        ({"provenance": "SYNTHETIC"}, False)])
def test_attribute_inspections_need_human_acceptance(ver, change, ok):
    res = ver.evaluate(_vart(_vrel(attribute_inspections=[{**_INSP, **change}])))
    assert (res["decision"]["decision"] == "PASS") is ok, res["decision"]["explanation"]
    if not ok:
        assert codes(res) & {"INSPECTION_NOT_ACCEPTED", "SYNTHETIC_AS_PHYSICAL"}


def test_synthetic_package_is_never_verified_qualification(ver):
    res = ver.evaluate(_vart(_vrel(), synthetic=True, release_stage="CONCEPT"))
    assert res["decision"]["decision"] == "BLOCK" and "SYNTHETIC_AS_PHYSICAL" in codes(res)


def test_research_only_reliability_and_licensed_ap_are_accepted(ver):
    p = _vrel(reliability={"label": "RESEARCH_ONLY_STATISTICAL_ILLUSTRATION", "release_eligible": False},
              prioritization_method="AIAG_VDA_AP_LICENSED", ap_license_ref="LIC-AP-2026-001")
    assert ver.evaluate(_vart(p))["decision"]["decision"] == "PASS"


def test_prd_hazards_must_reference_its_requirements(n0):
    n0.source("SRC-ENG", permitted_use=("PRODUCT_DEFINITION",))
    from conftest import rfq
    payload = {"prd_id": "PRD-9", "product_name": "PSU", "customer_name": "Acme", "product_category": "power_supply",
               "quantity": 10, "requirements": copy.deepcopy(rfq()["payload"]["requirements"]),
               "design_constraints": {}, "design_scopes": ["schematic_pcb"], "compliance_standards": ["IEC 62368-1"],
               "hazards": [{"hazard_id": "H-1", "description": "electric shock", "severity": 10,
                            "requirement_ids": ["R-001", "R-999"]}],
               "safety_critical_requirement_ids": ["R-404"], "authored_by": "pm"}
    res = n0.evaluate(_art("PRD-9", "PRD", payload, "PRODUCT_DEFINITION"))
    msgs = [v["message"] for v in res["decision"]["violated_rules"] if v["code"] == "CRITICAL_REFERENCE_INVALID"]
    assert res["decision"]["decision"] == "REWORK" and len(msgs) == 2


def test_parquet_logs_are_recognised_and_allowed():
    data = b"PAR1" + b"\x00" * 64 + b"PAR1"
    assert sniff_mime(data) == "application/vnd.apache.parquet"
    assert validate_upload(data, "ate_log.parquet", None, 1 << 20).ok
    assert not validate_upload(data, "ate_log.csv", None, 1 << 20).ok
