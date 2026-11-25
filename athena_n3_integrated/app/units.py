"""Deterministic SI normalization for engineering values (no model involvement).

``normalize(value, unit)`` → ``(si_value, si_unit, dimension)``; unknown units raise ``UnitError``.
Parameter names carry their dimension as a suffix (``output_voltage_v``, ``operating_temp_max_c``),
so a value whose unit disagrees with its parameter is caught as a dimension mismatch.

Temperatures normalize to degC (the SI-derived degree Celsius); °F and K convert exactly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Unit:
    dimension: str
    canonical: str
    factor: float
    offset: float = 0.0


_U: dict[str, Unit] = {}


def _add(dimension: str, canonical: str, table: dict[str, float]) -> None:
    for name, factor in table.items():
        _U[name] = Unit(dimension, canonical, factor)


_add("voltage", "V", {"V": 1, "mV": 1e-3, "uV": 1e-6, "µV": 1e-6, "kV": 1e3, "VDC": 1, "VAC": 1, "Vrms": 1})
_add("current", "A", {"A": 1, "mA": 1e-3, "uA": 1e-6, "µA": 1e-6, "kA": 1e3})
_add("power", "W", {"W": 1, "mW": 1e-3, "kW": 1e3, "MW": 1e6})
_add("resistance", "ohm", {"ohm": 1, "Ω": 1, "mohm": 1e-3, "mΩ": 1e-3, "kohm": 1e3, "kΩ": 1e3, "Mohm": 1e6, "MΩ": 1e6})
_add("frequency", "Hz", {"Hz": 1, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9})
_add("time", "s", {"s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9, "min": 60, "h": 3600})
_add("length", "m", {"m": 1, "mm": 1e-3, "um": 1e-6, "µm": 1e-6, "cm": 1e-2, "km": 1e3})
_add("mass", "kg", {"kg": 1, "g": 1e-3, "mg": 1e-6})
_add("capacitance", "F", {"F": 1, "mF": 1e-3, "uF": 1e-6, "µF": 1e-6, "nF": 1e-9, "pF": 1e-12})
_add("inductance", "H", {"H": 1, "mH": 1e-3, "uH": 1e-6, "µH": 1e-6, "nH": 1e-9})
_add("energy", "J", {"J": 1, "kJ": 1e3, "Wh": 3600, "kWh": 3.6e6})
_add("charge", "C", {"C": 1, "Ah": 3600, "mAh": 3.6})
_add("ratio", "1", {"1": 1, "ratio": 1, "%": 0.01, "pct": 0.01, "ppm": 1e-6})
_add("count", "count", {"count": 1, "pcs": 1, "ea": 1})
_U.update({"degC": Unit("temperature", "degC", 1), "°C": Unit("temperature", "degC", 1),
           "C": _U["C"],  # coulomb; temperatures must be written degC / °C to avoid ambiguity
           "K": Unit("temperature", "degC", 1, -273.15),
           "degF": Unit("temperature", "degC", 5 / 9, -32 * 5 / 9), "°F": Unit("temperature", "degC", 5 / 9, -32 * 5 / 9)})

# parameter-name suffix → dimension
SUFFIX_DIMENSION = {
    "v": "voltage", "a": "current", "w": "power", "ohm": "resistance", "hz": "frequency", "s": "time",
    "ms": "time", "m": "length", "mm": "length", "kg": "mass", "g": "mass", "f": "capacitance",
    "uh": "inductance", "j": "energy", "ah": "charge", "pct": "ratio", "ratio": "ratio", "ppm": "ratio",
    "c": "temperature", "count": "count", "qty": "count",
}
UNIT_ALIASES = {"volt": "V", "volts": "V", "amp": "A", "amps": "A", "ampere": "A", "watt": "W", "watts": "W",
                "Vdc": "VDC", "Vac": "VAC", "ohms": "ohm", "deg C": "degC", "C°": "degC", "celsius": "degC",
                "percent": "%", "khz": "kHz", "mhz": "MHz", "hz": "Hz", "mv": "mV", "ma": "mA", "kw": "kW",
                "mw": "mW"}


class UnitError(ValueError):
    pass


def canonical_unit_name(unit: Optional[str]) -> Optional[str]:
    if unit is None:
        return None
    u = unit.strip()
    return UNIT_ALIASES.get(u, UNIT_ALIASES.get(u.lower(), u))


def lookup(unit: str) -> Unit:
    u = canonical_unit_name(unit)
    if u not in _U:
        raise UnitError(f"unit {unit!r} is not in the SI registry")
    return _U[u]


def normalize(value: float, unit: str) -> tuple[float, str, str]:
    u = lookup(unit)
    return float(f"{value * u.factor + u.offset:.12g}"), u.canonical, u.dimension


def parameter_dimension(parameter: str) -> Optional[str]:
    """Dimension implied by the parameter's unit suffix (``..._v`` → voltage), if any."""
    parts = parameter.split("_")
    for n in (2, 1):
        if len(parts) > n:
            suffix = "_".join(parts[-n:]) if n == 2 else parts[-1]
            if suffix in SUFFIX_DIMENSION:
                return SUFFIX_DIMENSION[suffix]
    return None
