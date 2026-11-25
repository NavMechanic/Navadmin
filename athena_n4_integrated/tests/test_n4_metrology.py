"""Deterministic metrology: guard-banded verdicts (value ± U never PASSes across a limit), SI unit
conversion through N0's registry, and calibration validity at capture time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.n4.metrology import calibration_problems, convert, judge
from app.n4.models import AcceptanceLimit, N4CalibrationRecord, N4Instrument

NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
VOUT = AcceptanceLimit(characteristic="output_voltage", unit="V", lower=D("23.5"), upper=D("24.5"))


@pytest.mark.parametrize("value, u, unit, verdict", [
    ("24.00", "0.05", "V", "PASS"),                 # the demo reading
    ("24.48", "0.05", "V", "INDETERMINATE"),        # 24.53 crosses the 24.5 V maximum
    ("24480", "50", "mV", "INDETERMINATE"),         # same reading in mV
    ("24.45", "0.05", "V", "PASS"),                 # exactly on the guard band
    ("24.56", "0.05", "V", "FAIL"),                 # entirely above the limit
    ("23.52", "0.05", "V", "INDETERMINATE"),        # crosses the 23.5 V minimum
    ("23.40", "0.05", "V", "FAIL"),
])
def test_guard_banded_verdicts(value, u, unit, verdict):
    assert judge(D(value), unit, D(u), VOUT).verdict == verdict


def test_values_convert_exactly_into_the_limit_unit():
    assert convert(D("0.25"), "mA", "A") == D("0.00025")
    assert convert(D("100"), "Mohm", "ohm") == D("100000000")
    assert convert(D("25"), "degC", "K") == D("298.15")
    j = judge(D("850"), "Mohm", D("40"), AcceptanceLimit(characteristic="r", unit="ohm", lower=D("1e8")))
    assert j.verdict == "PASS" and j.value == D("850000000") and j.uncertainty == D("40000000")


def test_dimension_mismatch_is_never_a_pass():
    j = judge(D("5"), "A", D("0.1"), VOUT)
    assert j.verdict == "INDETERMINATE" and j.problems[0][0] == "DIMENSION_MISMATCH"
    assert judge(D("24"), "volts-ish", D("0.1"), VOUT).problems[0][0] == "UNIT_NOT_SI"


@pytest.mark.parametrize("u", [None, "0"])
def test_missing_uncertainty_is_never_a_pass(u):
    j = judge(D("24.00"), "V", D(u) if u else None, VOUT)
    assert j.verdict == "INDETERMINATE" and j.problems[0][0] == "UNCERTAINTY_MISSING"


def test_test_uncertainty_ratio_is_reported():
    assert judge(D("24.00"), "V", D("0.05"), VOUT).tur == D("10")
    assert judge(D("24.00"), "V", D("0.2"), VOUT).tur == D("2.5")


def _inst(serial="MY1"):
    return N4Instrument(instrument_id="DMM-1", tenant_id="acme", kind="DMM", manufacturer="Keysight Technologies",
                        model="34465A", serial=serial)


def _cal(**over):
    base = dict(calibration_id="CAL-1", tenant_id="acme", instrument_id="DMM-1", instrument_serial="MY1",
                certificate_number="C-1", calibration_lab="lab", calibrated_at=NOW - timedelta(days=100),
                calibration_due=NOW + timedelta(days=200), status="VALID", recorded_by="li", recorded_at=NOW)
    base.update(over)
    return N4CalibrationRecord(**base)


@pytest.mark.parametrize("cal, at, codes", [
    (_cal(), NOW - timedelta(days=1), set()),
    (_cal(calibration_due=NOW - timedelta(days=3)), NOW - timedelta(days=1), {"CALIBRATION_EXPIRED"}),
    (_cal(calibration_due=NOW - timedelta(days=1)), NOW - timedelta(days=1), {"CALIBRATION_EXPIRED"}),   # due == capture
    (_cal(calibrated_at=NOW - timedelta(hours=1)), NOW - timedelta(days=1), {"CALIBRATION_NOT_YET_VALID"}),
    (_cal(status="REVOKED"), NOW - timedelta(days=1), {"CALIBRATION_REVOKED"}),
    (_cal(instrument_serial="OTHER"), NOW - timedelta(days=1), {"CALIBRATION_MISMATCH"}),
    (_cal(), NOW + timedelta(days=1), {"CAPTURE_IN_FUTURE"}),
    (None, NOW - timedelta(days=1), {"CALIBRATION_MISSING"}),
])
def test_calibration_must_be_valid_at_capture(cal, at, codes):
    assert {c for c, _ in calibration_problems(_inst(), cal, at, NOW, 300)} == codes


def test_unregistered_instrument_has_no_calibration():
    assert [c for c, _ in calibration_problems(None, _cal(), NOW, NOW, 0)] == ["INSTRUMENT_UNREGISTERED"]
