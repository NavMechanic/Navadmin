"""Deterministic metrology: SI units, guard-banded verdicts and calibration validity.

* Units come from N0's SI registry (``app/units.py``). A value is converted into the unit of its
  acceptance limit with exact ``Decimal`` arithmetic, and dimensions must agree.
* Verdicts use the same rule N0 applies at release (``app.rules.guard_band_verdict``), with a guard
  band equal to the expanded uncertainty U: PASS only when value ± U lies inside the limits,
  INDETERMINATE when it crosses a limit (never PASS), FAIL when it lies entirely outside.
* Calibration is valid for a measurement only if the certificate belongs to that instrument
  (matching serial), is not revoked, and ``calibrated_at ≤ captured_at < calibration_due``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from ..rules import guard_band_verdict
from ..units import UnitError, lookup
from .models import AcceptanceLimit, N4CalibrationRecord, N4Instrument


class MetrologyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def unit_info(unit: str) -> tuple[str, str]:
    try:
        u = lookup(unit)
    except UnitError as exc:
        raise MetrologyError("UNIT_NOT_SI", str(exc)) from None
    return u.canonical, u.dimension


def convert(value: Decimal, from_unit: str, to_unit: str) -> Decimal:
    a, b = lookup(from_unit), lookup(to_unit)
    if a.dimension != b.dimension:
        raise MetrologyError("DIMENSION_MISMATCH", f"{from_unit} ({a.dimension}) cannot be compared with {to_unit} "
                                                   f"({b.dimension})")
    si = value * Decimal(repr(a.factor)) + Decimal(repr(a.offset))
    return (si - Decimal(repr(b.offset))) / Decimal(repr(b.factor))


def convert_span(span: Decimal, from_unit: str, to_unit: str) -> Decimal:
    """Convert an uncertainty (a difference): offsets cancel."""
    a, b = lookup(from_unit), lookup(to_unit)
    if a.dimension != b.dimension:
        raise MetrologyError("DIMENSION_MISMATCH", f"{from_unit} vs {to_unit}")
    return span * Decimal(repr(a.factor)) / Decimal(repr(b.factor))


def _norm(d: Decimal) -> Decimal:
    q = d.normalize()
    return q if q.as_tuple().exponent <= 0 else q.quantize(Decimal(1))


@dataclass
class Judged:
    verdict: str
    value: Decimal                     # in the limit unit
    uncertainty: Optional[Decimal]     # in the limit unit
    unit: str
    problems: list[tuple[str, str]] = field(default_factory=list)
    tur: Optional[Decimal] = None


def judge(value: Decimal, unit: str, uncertainty: Optional[Decimal], limit: AcceptanceLimit) -> Judged:
    """Guard-banded verdict of one value against one acceptance limit (converted to the limit unit)."""
    problems: list[tuple[str, str]] = []
    try:
        unit_info(unit)
        unit_info(limit.unit)
        v = _norm(convert(value, unit, limit.unit))
        u = _norm(convert_span(uncertainty, unit, limit.unit)) if uncertainty is not None else None
    except MetrologyError as exc:
        return Judged("INDETERMINATE", value, uncertainty, unit, [(exc.code, exc.message)])
    if u is None or u <= 0:
        return Judged("INDETERMINATE", v, u, limit.unit,
                      [("UNCERTAINTY_MISSING", "a measurement needs a positive expanded uncertainty")])
    verdict = guard_band_verdict(v, u, limit.lower, limit.upper)
    tur = None
    if limit.lower is not None and limit.upper is not None:
        tur = (limit.upper - limit.lower) / (2 * u)
    return Judged(verdict, v, u, limit.unit, problems, tur)


def calibration_problems(instrument: Optional[N4Instrument], cal: Optional[N4CalibrationRecord],
                         captured_at: datetime, now: datetime, skew_s: int) -> list[tuple[str, str]]:
    out = []
    if captured_at > now + timedelta(seconds=skew_s):
        out.append(("CAPTURE_IN_FUTURE", f"captured_at {captured_at.isoformat()} is in the future"))
    if instrument is None:
        out.append(("INSTRUMENT_UNREGISTERED", "the instrument is not in the N4 instrument register"))
        return out
    if cal is None:
        out.append(("CALIBRATION_MISSING", f"no calibration record for {instrument.instrument_id}"))
        return out
    if cal.instrument_id != instrument.instrument_id or cal.instrument_serial != instrument.serial:
        out.append(("CALIBRATION_MISMATCH", f"certificate {cal.certificate_number} is for another instrument"))
    if cal.status != "VALID":
        out.append(("CALIBRATION_REVOKED", f"calibration {cal.calibration_id} is {cal.status}"))
    if captured_at < cal.calibrated_at:
        out.append(("CALIBRATION_NOT_YET_VALID", f"captured before calibration on {cal.calibrated_at.date()}"))
    if not captured_at < cal.calibration_due:
        out.append(("CALIBRATION_EXPIRED", f"calibration was due {cal.calibration_due.isoformat()}, measurement "
                                           f"captured {captured_at.isoformat()}"))
    return out
