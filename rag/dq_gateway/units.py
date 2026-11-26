"""SI-compatible unit registry and normalization (rule DQ04).

Every accepted unit maps to a canonical SI (or SI-accepted) unit with an exact
conversion. Units outside the registry — including imperial units — are
rejected rather than guessed, so producers must convert explicitly.

    normalize(25.4, "mm")   -> (0.0254, "m")
    normalize(3.0, "bar")   -> (300000.0, "Pa")
    normalize(300.0, "K")   -> (26.85, "degC")
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnitDef:
    canonical: str
    factor: float
    offset: float = 0.0  # canonical = value * factor + offset (affine only for temperature)


_PREFIXED = {
    # length
    "m": ("m", 1.0), "km": ("m", 1e3), "cm": ("m", 1e-2), "mm": ("m", 1e-3), "um": ("m", 1e-6), "µm": ("m", 1e-6),
    "nm": ("m", 1e-9),
    # mass
    "kg": ("kg", 1.0), "g": ("kg", 1e-3), "mg": ("kg", 1e-6), "ug": ("kg", 1e-9), "µg": ("kg", 1e-9),
    # time
    "s": ("s", 1.0), "ms": ("s", 1e-3), "us": ("s", 1e-6), "µs": ("s", 1e-6), "ns": ("s", 1e-9),
    "min": ("s", 60.0), "h": ("s", 3600.0),
    # electrical
    "A": ("A", 1.0), "mA": ("A", 1e-3), "uA": ("A", 1e-6), "µA": ("A", 1e-6),
    "V": ("V", 1.0), "mV": ("V", 1e-3), "kV": ("V", 1e3), "uV": ("V", 1e-6), "µV": ("V", 1e-6),
    "W": ("W", 1.0), "mW": ("W", 1e-3), "kW": ("W", 1e3),
    "ohm": ("ohm", 1.0), "Ω": ("ohm", 1.0), "mohm": ("ohm", 1e-3), "mΩ": ("ohm", 1e-3),
    "kohm": ("ohm", 1e3), "kΩ": ("ohm", 1e3), "Mohm": ("ohm", 1e6), "MΩ": ("ohm", 1e6),
    "F": ("F", 1.0), "uF": ("F", 1e-6), "µF": ("F", 1e-6), "nF": ("F", 1e-9), "pF": ("F", 1e-12),
    "H": ("H", 1.0), "mH": ("H", 1e-3), "uH": ("H", 1e-6), "µH": ("H", 1e-6),
    "C": ("C", 1.0), "J": ("J", 1.0), "kJ": ("J", 1e3),
    # frequency
    "Hz": ("Hz", 1.0), "kHz": ("Hz", 1e3), "MHz": ("Hz", 1e6), "GHz": ("Hz", 1e9),
    # mechanics
    "N": ("N", 1.0), "kN": ("N", 1e3), "mN": ("N", 1e-3),
    "N·m": ("N·m", 1.0), "N*m": ("N·m", 1.0), "Nm": ("N·m", 1.0), "mN·m": ("N·m", 1e-3), "kN·m": ("N·m", 1e3),
    "Pa": ("Pa", 1.0), "kPa": ("Pa", 1e3), "MPa": ("Pa", 1e6), "GPa": ("Pa", 1e9), "bar": ("Pa", 1e5),
    "mbar": ("Pa", 1e2),
    # dimensionless / logarithmic (SI-accepted)
    "1": ("1", 1.0), "ratio": ("1", 1.0), "%": ("1", 0.01), "pct": ("1", 0.01), "%RH": ("1", 0.01),
    "ppm": ("1", 1e-6), "dB": ("dB", 1.0), "dBm": ("dBm", 1.0),
    # other SI base
    "mol": ("mol", 1.0), "cd": ("cd", 1.0), "lm": ("lm", 1.0), "lx": ("lx", 1.0),
}

UNITS: dict[str, UnitDef] = {k: UnitDef(c, f) for k, (c, f) in _PREFIXED.items()}
UNITS.update({
    "degC": UnitDef("degC", 1.0), "°C": UnitDef("degC", 1.0),
    "K": UnitDef("degC", 1.0, -273.15),
})


class UnitError(ValueError):
    pass


def is_si_compatible(unit: str) -> bool:
    return isinstance(unit, str) and unit.strip() in UNITS


def normalize(value: float, unit: str) -> tuple[float, str]:
    """Convert a value to its canonical SI unit. Raises UnitError for unknown units."""
    u = UNITS.get(unit.strip()) if isinstance(unit, str) else None
    if u is None:
        raise UnitError(f"unit {unit!r} is not in the SI-compatible registry")
    return float(f"{value * u.factor + u.offset:.12g}"), u.canonical
