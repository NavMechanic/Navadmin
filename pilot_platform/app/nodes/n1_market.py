"""N1 Market — evidence-based SAM (gate J: "No unsupported TAM→SAM conversion").

An opportunity enters the SAM only with a named account, at least one cited evidence
item (source + summary), a positive value, and a basis other than a share of TAM.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _reasons(opp: dict) -> list[str]:
    reasons = []
    if not str(opp.get("account", "")).strip():
        reasons.append("no named account")
    evidence = [e for e in opp.get("evidence", []) if str(e.get("source", "")).strip() and str(e.get("summary", "")).strip()]
    if not evidence:
        reasons.append("no cited evidence (each item needs source and summary)")
    if opp.get("basis") == "tam_share":
        reasons.append("a share of TAM is not evidence of serviceable demand")
    try:
        if Decimal(str(opp.get("annual_value", "0"))) <= 0:
            reasons.append("annual_value must be positive")
    except InvalidOperation:
        reasons.append("annual_value is not a number")
    return reasons


def evaluate_opportunities(opportunities: list[dict]) -> dict:
    sam, rejected = [], []
    for opp in opportunities:
        reasons = _reasons(opp)
        (rejected if reasons else sam).append({**opp, "reasons": reasons} if reasons else opp)
    sam.sort(key=lambda o: Decimal(str(o["annual_value"])), reverse=True)
    total = sum((Decimal(str(o["annual_value"])) for o in sam), Decimal("0"))
    return {"sam": sam, "rejected": rejected, "sam_total": str(total), "accepted": len(sam), "rejected_count": len(rejected)}


def sam_supported(content: dict) -> tuple[bool, list[str]]:
    """Re-validate a stored MARKET artifact (defends against hand-written artifacts)."""
    problems = [f"{o.get('account') or '<unnamed>'}: {', '.join(r)}" for o in content.get("sam", []) if (r := _reasons(o))]
    return not problems, problems
