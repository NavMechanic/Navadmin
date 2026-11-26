"""Weighted quality score (advisory) and the gate decision matrix.

    weighted = 0.20·completeness + 0.20·validity + 0.15·consistency
             + 0.15·provenance  + 0.10·freshness + 0.20·traceability      (each 0–100)

Dimensions are measurements of the data (fill ratios, reconciliation matches, age against the
SLA, resolved references) — not a pass-rate of the rules — so the score carries information the
rules don't. It is advisory: the decision matrix applies hard controls first, and no score can
compensate for a failed rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .rules import RuleContext, RuleResult

WEIGHTS = {"completeness": 0.20, "validity": 0.20, "consistency": 0.15, "provenance": 0.15,
           "freshness": 0.10, "traceability": 0.20}
SCORE_THRESHOLD = 90.0
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


@dataclass
class ScoreCard:
    dimensions: dict[str, float]
    weighted_score: float
    notes: dict = field(default_factory=dict)


def _ratio(pair, default: float = 1.0) -> float:
    if not pair:
        return default
    done, total = pair
    return default if total == 0 else max(0.0, min(1.0, done / total))


def score(ctx: RuleContext) -> ScoreCard:
    m = ctx.metrics
    # completeness: critical fields dominate; optional schema fields add the last 20 %
    optional_ratio = 1.0
    if ctx.spec is not None:
        optional = [f for f in ctx.spec.model.model_fields if f not in ctx.spec.critical]
        if optional:
            optional_ratio = sum(1 for f in optional if f in ctx.payload and ctx.payload[f] not in (None, "", [], {})) \
                / len(optional)
    completeness = 100 * (0.8 * _ratio(m.get("critical")) + 0.2 * optional_ratio)
    validity = 0.0 if ctx.schema_errors else 100 * _ratio(m.get("units"))
    consistency = 100 * _ratio(m.get("consistency"))
    prov_checks = [
        "@" in ctx.manifest.model_or_tool or ctx.manifest.tool_kind in ("sensor", "human", "vendor"),
        _ratio(m.get("integrity"), 0.0) == 1.0,
        bool(ctx.manifest.evidence_ids),
        ctx.manifest.vendor_id is not None or ctx.manifest.tool_kind != "vendor",
    ]
    provenance = 100 * sum(prov_checks) / len(prov_checks)
    age, sla = m.get("freshness", (0.0, 1.0))
    freshness = 100.0 if age <= 0.5 * sla else max(0.0, 100 * (1 - (age - 0.5 * sla) / (0.5 * sla)))
    traced = m.get("traced") or m.get("claims")
    traceability = 100 * (0.5 * _ratio(m.get("refs"), 0.0) + 0.5 * _ratio(traced))
    dims = {"completeness": completeness, "validity": validity, "consistency": consistency,
            "provenance": provenance, "freshness": freshness, "traceability": traceability}
    dims = {k: round(v, 2) for k, v in dims.items()}
    weighted = round(sum(WEIGHTS[k] * v for k, v in dims.items()), 2)
    return ScoreCard(dims, weighted, {"age_seconds": age, "sla_seconds": sla})


def decide_gate(results: list[RuleResult], hard_policy_results: dict[str, str], weighted_score: float,
                approval_verified: bool, threshold: float = SCORE_THRESHOLD) -> tuple[str, list[str]]:
    """The gate decision matrix, applied strictly in order."""
    failed = [r for r in results if r.applicable and not r.passed]
    declared_bad = sorted(k for k, v in hard_policy_results.items() if v in ("FAIL", "UNKNOWN"))
    if q := [r.rule_id for r in failed if r.action_on_fail == "QUARANTINE"]:
        return "QUARANTINE", [f"quarantine rules failed: {q}"]
    blocks = [r.rule_id for r in failed if r.action_on_fail == "BLOCK"]
    if blocks or declared_bad:
        return "BLOCK", [f"blocking rules failed: {blocks}"] * bool(blocks) + \
            [f"hard policy FAIL/UNKNOWN: {declared_bad}"] * bool(declared_bad)
    reworks = [r.rule_id for r in failed if r.action_on_fail == "REWORK"]
    if reworks or weighted_score < threshold:
        return "REWORK", [f"rework rules failed: {reworks}"] * bool(reworks) + \
            [f"weighted score {weighted_score} < {threshold}"] * (weighted_score < threshold)
    if not approval_verified:
        return "HOLD", ["all controls pass; awaiting human sign-off"]
    return "RELEASED", ["all controls pass; verified human sign-off"]
