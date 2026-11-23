"""N3 Design — design-pack review (gate M: "Simulation assumptions + engineer review").

EDA/CAE tools (KiCad, SPICE, thermal, DFM) run outside the platform, directly or via a
vendor adapter; this node records their outputs and enforces the review rules:
ERC and DRC passed, every simulation states its assumptions and a pass/fail result,
and an engineer other than the author has reviewed the pack.
"""
from __future__ import annotations


def junction_temperature(power_w: float, theta_ja_c_per_w: float, ambient_c: float) -> float:
    """Tj = Ta + P × θJA (steady state, single thermal resistance)."""
    return round(ambient_c + power_w * theta_ja_c_per_w, 3)


def review_design_pack(pack: dict) -> dict:
    issues, sims = [], []
    author, reviewer = pack.get("author"), pack.get("reviewed_by")
    if not author:
        issues.append("author missing")
    if not reviewer:
        issues.append("engineer review missing (reviewed_by)")
    elif reviewer == author:
        issues.append("reviewer must differ from author")
    for flag in ("erc_passed", "drc_passed"):
        if pack.get(flag) is not True:
            issues.append(f"{flag} is not true")
    if not pack.get("simulations"):
        issues.append("no simulations recorded")
    for i, sim in enumerate(pack.get("simulations", [])):
        label = f"simulation[{i}] ({sim.get('type', '?')})"
        result = dict(sim.get("result", {}))
        if not sim.get("assumptions"):
            issues.append(f"{label}: assumptions not stated")
        inputs = sim.get("inputs", {})
        if sim.get("type") == "thermal" and {"power_w", "theta_ja_c_per_w", "ambient_c", "tj_max_c"} <= set(inputs):
            tj = junction_temperature(inputs["power_w"], inputs["theta_ja_c_per_w"], inputs["ambient_c"])
            result.update({"tj_c": tj, "margin_c": round(inputs["tj_max_c"] - tj, 3), "passed": tj <= inputs["tj_max_c"]})
        if not isinstance(result.get("passed"), bool):
            issues.append(f"{label}: no pass/fail result")
        elif not result["passed"]:
            issues.append(f"{label}: failed")
        sims.append({**sim, "result": result})
    return {"passed": not issues, "issues": issues, "simulations": sims}
