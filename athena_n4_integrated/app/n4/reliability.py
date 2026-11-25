"""Reliability as a research-only statistical illustration (never a release input or a claim).

Exponential (constant failure rate) model on pooled unit-hours, with the one-sided lower confidence
bound for a time-terminated test: MTBF_L = 2T / χ²(C; 2r + 2). The χ² quantile uses the
Wilson–Hilferty approximation (stdlib only). This stays an illustration until longitudinal unit
genealogy (N8), field operating hours and confirmed failure cohorts exist.
"""
from __future__ import annotations

from statistics import NormalDist
from typing import Optional

PREREQUISITES = ["longitudinal unit genealogy (N8) for every unit", "field operating hours per unit",
                 "confirmed failure cohorts with verified root cause"]


def chi2_quantile(p: float, dof: int) -> float:
    z = NormalDist().inv_cdf(p)
    k = float(dof)
    return k * (1 - 2 / (9 * k) + z * (2 / (9 * k)) ** 0.5) ** 3


def mtbf(total_hours: float, failures: int, confidence: float) -> tuple[Optional[float], Optional[float]]:
    point = total_hours / failures if failures else None
    lower = 2 * total_hours / chi2_quantile(confidence, 2 * failures + 2) if total_hours > 0 else None
    return point, lower
