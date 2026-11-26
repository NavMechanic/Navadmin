"""N5 Sourcing — BOM normalisation and supplier bid comparison (gate O: "2 comparable valid bids where attainable").

A bid is comparable when it is in the requested currency, still valid on the as-of date,
and prices every BOM line. Fewer than two comparable bids pass only with a written
single-source justification ("where attainable").
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ..core.errors import ValidationFailed

CENT = Decimal("0.0001")


def normalize_mpn(mpn: str) -> str:
    return re.sub(r"\s+", "", str(mpn)).upper()


def normalize_bom(lines: list[dict]) -> dict:
    merged: dict[tuple[str, str], dict] = {}
    errors, warnings = [], []
    for i, line in enumerate(lines):
        mpn = normalize_mpn(line.get("mpn", ""))
        if not mpn:
            errors.append(f"line {i}: missing MPN")
            continue
        qty = line.get("qty_per_unit")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
            errors.append(f"line {i} ({mpn}): qty_per_unit must be positive")
            continue
        refdes = [r.strip().upper() for r in line.get("refdes", []) if r.strip()]
        if refdes and len(refdes) != qty:
            warnings.append(f"{mpn}: {len(refdes)} reference designators for qty {qty}")
        key = (mpn, str(line.get("manufacturer", "")).strip().upper())
        if key in merged:
            merged[key]["qty_per_unit"] += qty
            merged[key]["refdes"] += refdes
            warnings.append(f"{mpn}: duplicate lines merged")
        else:
            merged[key] = {"mpn": mpn, "manufacturer": key[1], "description": line.get("description", ""),
                           "qty_per_unit": qty, "refdes": refdes}
    return {"lines": list(merged.values()), "errors": errors, "warnings": warnings}


def evaluate_bids(bom_lines: list[dict], bids: list[dict], currency: str, as_of: date, build_qty: int) -> list[dict]:
    out = []
    for bid in bids:
        reasons = []
        prices = {normalize_mpn(k): v for k, v in bid.get("unit_prices", {}).items()}
        if bid.get("currency") != currency:
            reasons.append(f"currency {bid.get('currency')} != {currency}")
        try:
            if date.fromisoformat(bid["valid_until"]) < as_of:
                reasons.append(f"expired {bid['valid_until']}")
        except (KeyError, ValueError):
            reasons.append("valid_until missing or not ISO date")
        missing = [l["mpn"] for l in bom_lines if l["mpn"] not in prices]
        if missing:
            reasons.append(f"does not price {missing}")
        unit_cost = None
        if not missing:
            try:
                unit_cost = sum((Decimal(str(prices[l["mpn"]])) * Decimal(str(l["qty_per_unit"])) for l in bom_lines),
                                Decimal("0")).quantize(CENT)
            except InvalidOperation:
                reasons.append("non-numeric price")
        out.append({"supplier": bid.get("supplier"), "currency": bid.get("currency"),
                    "lead_time_days": bid.get("lead_time_days"), "valid_until": bid.get("valid_until"),
                    "comparable": not reasons, "reasons": reasons,
                    "unit_bom_cost": str(unit_cost) if unit_cost is not None else None,
                    "extended_cost": str((unit_cost * build_qty).quantize(CENT)) if unit_cost is not None else None})
    return out


def run_sourcing(bom: list[dict], bids: list[dict], currency: str, as_of: date, build_qty: int,
                 single_source_justification: str | None = None) -> dict:
    if build_qty <= 0:
        raise ValidationFailed("build_qty must be positive")
    norm = normalize_bom(bom)
    if norm["errors"]:
        raise ValidationFailed("BOM has errors", errors=norm["errors"])
    evaluated = evaluate_bids(norm["lines"], bids, currency, as_of, build_qty)
    comparable = [b for b in evaluated if b["comparable"]]
    best = min(comparable, key=lambda b: (Decimal(b["unit_bom_cost"]), b["lead_time_days"] or 0), default=None)
    justified = bool(single_source_justification and single_source_justification.strip())
    return {"bom": norm, "bids": evaluated, "comparable_count": len(comparable), "best_bid": best,
            "currency": currency, "build_qty": build_qty, "as_of": as_of.isoformat(),
            "single_source_justification": single_source_justification,
            "gate_passed": best is not None and (len(comparable) >= 2 or justified)}
