"""N7 Finance — deterministic cost, margin and working-capital engine (gate Q: "Deterministic calculations, no LLM math").

All money is Decimal; results are rounded half-up to cents only at the output. Every
result carries a hash over its normalised inputs and outputs so it can be re-derived
and verified later. Prices are ex-GST; GST is shown separately.
"""
from __future__ import annotations

import hashlib
import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..core.audit import canonical_json
from ..core.errors import ValidationFailed

CENT = Decimal("0.01")
MONEY_INPUTS = ("unit_bom_cost", "nre", "labor_rate_per_hour", "ai_cost", "test_cost_per_unit")
NUMBER_INPUTS = ("labor_hours", "target_margin", "annual_finance_rate", "finance_days", "gst_rate")


def _d(inputs: dict, key: str, default: str | None = None) -> Decimal:
    raw = inputs.get(key, default)
    if raw is None:
        raise ValidationFailed(f"missing input {key}")
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise ValidationFailed(f"{key} is not a number")
    if value < 0:
        raise ValidationFailed(f"{key} must not be negative")
    return value


def _money(x: Decimal) -> str:
    return str(x.quantize(CENT, rounding=ROUND_HALF_UP))


def cost_model(inputs: dict, default_gst_rate: str = "0.18") -> dict:
    qty = inputs.get("build_qty")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise ValidationFailed("build_qty must be a positive integer")
    v = {k: _d(inputs, k, "0") for k in MONEY_INPUTS + ("labor_hours", "annual_finance_rate", "finance_days")}
    margin = _d(inputs, "target_margin")
    gst_rate = _d(inputs, "gst_rate", default_gst_rate)
    if margin >= 1:
        raise ValidationFailed("target_margin must be below 1 (it is a share of the ex-GST price)")
    q = Decimal(qty)

    bom = v["unit_bom_cost"] * q
    labor = v["labor_hours"] * v["labor_rate_per_hour"]
    tests = v["test_cost_per_unit"] * q
    variable = bom + tests
    direct = variable + labor + v["ai_cost"]
    working_capital = (direct + v["nre"]) * v["annual_finance_rate"] * v["finance_days"] / Decimal(365)
    total_cost = direct + v["nre"] + working_capital
    price_ex_gst = total_cost / (Decimal(1) - margin)
    unit_price = price_ex_gst / q
    unit_variable = variable / q
    contribution = unit_price - unit_variable
    break_even = math.ceil((v["nre"] + labor + v["ai_cost"] + working_capital) / contribution) if contribution > 0 else None

    normalised = {"build_qty": qty, **{k: str(val) for k, val in v.items()}, "target_margin": str(margin), "gst_rate": str(gst_rate)}
    outputs = {
        "bom_cost": _money(bom), "labor_cost": _money(labor), "test_cost": _money(tests), "ai_cost": _money(v["ai_cost"]),
        "nre": _money(v["nre"]), "working_capital_cost": _money(working_capital), "total_cost": _money(total_cost),
        "price_ex_gst": _money(price_ex_gst), "gst": _money(price_ex_gst * gst_rate),
        "price_inc_gst": _money(price_ex_gst * (1 + gst_rate)), "unit_price_ex_gst": _money(unit_price),
        "unit_contribution": _money(contribution), "gross_margin": _money(price_ex_gst - total_cost),
        "break_even_units": break_even,
    }
    digest = hashlib.sha256(canonical_json({"inputs": normalised, "outputs": outputs}).encode()).hexdigest()
    return {"inputs": normalised, "outputs": outputs, "calculation_hash": digest, "engine": "n7-decimal-v1"}


def verify(record: dict, default_gst_rate: str = "0.18") -> bool:
    """Recompute a stored costing from its own inputs and compare."""
    inputs = dict(record["inputs"])
    return cost_model(inputs, default_gst_rate)["calculation_hash"] == record.get("calculation_hash")
