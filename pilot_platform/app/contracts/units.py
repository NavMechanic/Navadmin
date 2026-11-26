"""Engineering-unit parsing and SI normalization.

    parse_quantity("150 mA")  -> Quantity(0.15, "A")
    parse_quantity("3V3")     -> Quantity(3.3, "V")
    parse_quantity("1.2 kW")  -> Quantity(1200.0, "W")
    parse_quantity("85 mm")   -> Quantity(0.085, "m")
    parse_temperature("185 F") -> Quantity(85.0, "degC")
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
BASE = {"V": "V", "A": "A", "W": "W", "Hz": "Hz", "s": "s", "m": "m", "g": "kg", "Ω": "ohm", "ohm": "ohm", "F": "F"}
NUMBER = r"[-+]?\d[\d,]*(?:\.\d+)?"

_QTY = re.compile(rf"^\s*({NUMBER})\s*([pnuµμmkKMG]?)(Hz|ohm|Ω|V|A|W|s|m|g|F)\s*$")
_RADIX = re.compile(r"^\s*(\d+)([VRk])(\d+)\s*$")  # 3V3 -> 3.3 V, 4R7 -> 4.7 ohm, 4k7 -> 4700 ohm
_TEMP = re.compile(rf"^\s*({NUMBER})\s*°?\s*([CF])\s*$")


class UnitError(ValueError):
    pass


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str

    def to_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit}


def clean_number(text: str) -> float:
    return float(text.replace(",", "").replace("−", "-"))


def _tidy(x: float) -> float:
    return float(f"{x:.12g}")


def parse_quantity(text: str, expect: str | None = None) -> Quantity:
    text = text.replace("−", "-")
    m = _RADIX.match(text)
    if m:
        whole, sym, frac = m.groups()
        base = float(f"{whole}.{frac}")
        q = Quantity(base, "V") if sym == "V" else Quantity(_tidy(base * (1e3 if sym == "k" else 1)), "ohm")
    else:
        m = _QTY.match(text)
        if not m:
            raise UnitError(f"cannot parse quantity {text!r}")
        number, prefix, unit = m.groups()
        value = clean_number(number) * PREFIX[prefix]
        if unit == "g":
            value /= 1000.0  # grams -> kilograms
        q = Quantity(_tidy(value), BASE[unit])
    if expect and q.unit != expect:
        raise UnitError(f"expected unit {expect}, got {q.unit} in {text!r}")
    return q


def parse_temperature(text: str) -> Quantity:
    m = _TEMP.match(text.replace("−", "-"))
    if not m:
        raise UnitError(f"cannot parse temperature {text!r}")
    value, scale = clean_number(m.group(1)), m.group(2)
    return Quantity(_tidy((value - 32) * 5 / 9 if scale == "F" else value), "degC")


def to_celsius(value: float, scale: str | None) -> float:
    return _tidy((value - 32) * 5 / 9) if scale == "F" else _tidy(value)


LENGTH_FACTOR = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "in": 0.0254}


def length_to_m(value: float, unit: str) -> float:
    return _tidy(value * LENGTH_FACTOR[unit])
