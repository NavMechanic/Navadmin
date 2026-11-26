"""CPM / PERT network engine (pure functions, no database).

* PERT expected duration te = (o + 4m + p) / 6, variance = ((p - o) / 6)^2
* Forward pass: ES = max(EF of predecessors) — the AND-join; EF = ES + d
* Backward pass: LF = min(LS of successors) or project end; LS = LF - d
* Total float = LS - ES; critical when float == 0 (within EPS)
* A link is "driving" when EF(pred) == ES(succ)
* P80 by normal approximation over the critical activities, plus an optional
  Monte Carlo (Beta-PERT sampling) that also captures merge bias from
  near-critical parallel paths, which the normal approximation ignores.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Mapping

EPS = 1e-6
Z = {0.5: 0.0, 0.8: 0.8416, 0.9: 1.2816, 0.95: 1.6449}


class NetworkError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    id: str
    pred: tuple[str, ...]
    o: float
    m: float
    p: float


def pert(o: float, m: float, p: float) -> tuple[float, float]:
    if not (0 <= o <= m <= p):
        raise NetworkError(f"PERT estimates must satisfy 0 <= o <= m <= p (got {o}, {m}, {p})")
    return (o + 4 * m + p) / 6, ((p - o) / 6) ** 2


def topological_order(preds: Mapping[str, Iterable[str]]) -> list[str]:
    """Kahn's algorithm; stable w.r.t. input order. Raises on unknown predecessors or cycles."""
    preds = {k: list(v) for k, v in preds.items()}
    for node, ps in preds.items():
        missing = [p for p in ps if p not in preds]
        if missing:
            raise NetworkError(f"{node} depends on unknown activities {missing}")
        if node in ps:
            raise NetworkError(f"{node} depends on itself")
    indeg = {k: len(set(v)) for k, v in preds.items()}
    succ: dict[str, list[str]] = {k: [] for k in preds}
    for node, ps in preds.items():
        for p in set(ps):
            succ[p].append(node)
    ready = [k for k in preds if indeg[k] == 0]
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for s in succ[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)
    if len(order) != len(preds):
        cyclic = sorted(k for k, d in indeg.items() if d > 0)
        raise NetworkError(f"dependency cycle among {cyclic}")
    return order


@dataclass
class ScheduledActivity:
    id: str
    duration: float
    variance: float
    es: float
    ef: float
    ls: float
    lf: float
    total_float: float
    free_float: float
    critical: bool
    predecessors: list[str]
    successors: list[str]


@dataclass
class Schedule:
    activities: dict[str, ScheduledActivity]
    order: list[str]
    duration: float
    critical: list[str] = field(default_factory=list)

    @property
    def critical_variance(self) -> float:
        return sum(self.activities[a].variance for a in self.critical)

    @property
    def sigma(self) -> float:
        return math.sqrt(self.critical_variance)

    def percentile(self, q: float) -> float:
        if q not in Z:
            raise ValueError(f"supported percentiles: {sorted(Z)}")
        return self.duration + Z[q] * self.sigma

    def near_critical(self, threshold: float = 1.2) -> list[str]:
        return [a for a in self.order if not self.activities[a].critical and self.activities[a].total_float <= threshold]

    def links(self) -> list[dict]:
        out = []
        for sid in self.order:
            s = self.activities[sid]
            for pid in s.predecessors:
                p = self.activities[pid]
                driving = abs(p.ef - s.es) < EPS
                kind = "critical" if driving and p.critical and s.critical else "driving" if driving else "non_driving"
                out.append({"from": pid, "to": sid, "kind": kind, "lag": round(s.es - p.ef, 6)})
        return out

    def summary(self) -> dict:
        return {
            "expected_duration_days": round(self.duration, 4),
            "expected_duration_weeks": math.ceil(self.duration / 5 - EPS),
            "critical_path": self.critical,
            "critical_count": len(self.critical),
            "activity_count": len(self.order),
            "critical_sigma_days": round(self.sigma, 4),
            "p80_days_normal_approx": round(self.percentile(0.8), 4),
            "near_critical": self.near_critical(),
        }


def _clean(x: float) -> float:
    return 0.0 if abs(x) < EPS else round(x, 9)


def compute_schedule(nodes: Iterable[Node], durations: Mapping[str, float] | None = None) -> Schedule:
    nodes = list(nodes)
    by_id = {n.id: n for n in nodes}
    if len(by_id) != len(nodes):
        raise NetworkError("duplicate activity ids")
    order = topological_order({n.id: n.pred for n in nodes})
    succ: dict[str, list[str]] = {n.id: [] for n in nodes}
    for n in nodes:
        for p in n.pred:
            succ[p].append(n.id)

    dur, var = {}, {}
    for n in nodes:
        te, v = pert(n.o, n.m, n.p)
        dur[n.id] = float(durations[n.id]) if durations and n.id in durations else te
        var[n.id] = 0.0 if durations and n.id in durations else v

    es, ef = {}, {}
    for a in order:
        es[a] = max((ef[p] for p in by_id[a].pred), default=0.0)
        ef[a] = es[a] + dur[a]
    end = max(ef.values(), default=0.0)

    ls, lf = {}, {}
    for a in reversed(order):
        lf[a] = min((ls[s] for s in succ[a]), default=end)
        ls[a] = lf[a] - dur[a]

    acts = {}
    for a in order:
        tf = _clean(ls[a] - es[a])
        ff = _clean(min((es[s] for s in succ[a]), default=end) - ef[a])
        acts[a] = ScheduledActivity(
            id=a, duration=dur[a], variance=var[a], es=es[a], ef=ef[a], ls=ls[a], lf=lf[a],
            total_float=tf, free_float=ff, critical=abs(tf) < EPS,
            predecessors=list(by_id[a].pred), successors=succ[a],
        )
    return Schedule(activities=acts, order=order, duration=end, critical=[a for a in order if acts[a].critical])


def trace(nodes: Iterable[Node], start: str, direction: str) -> list[str]:
    """All activities upstream (`up`) or downstream (`down`) of `start`, excluding it."""
    nodes = list(nodes)
    preds = {n.id: list(n.pred) for n in nodes}
    if start not in preds:
        raise NetworkError(f"unknown activity {start}")
    succ: dict[str, list[str]] = {k: [] for k in preds}
    for k, ps in preds.items():
        for p in ps:
            succ[p].append(k)
    edges = preds if direction == "up" else succ
    seen, stack = set(), [start]
    while stack:
        for nxt in edges[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    order = topological_order(preds)
    return [a for a in order if a in seen]


def _beta_pert_sample(rng: random.Random, o: float, m: float, p: float) -> float:
    if p - o < EPS:
        return m
    alpha = 1 + 4 * (m - o) / (p - o)
    beta = 1 + 4 * (p - m) / (p - o)
    return o + rng.betavariate(alpha, beta) * (p - o)


def monte_carlo(nodes: Iterable[Node], iterations: int = 5000, seed: int = 7) -> dict:
    """Sample Beta-PERT durations; return finish percentiles and per-activity criticality index."""
    nodes = list(nodes)
    if not 100 <= iterations <= 50_000:
        raise ValueError("iterations must be between 100 and 50000")
    rng = random.Random(seed)
    finishes: list[float] = []
    critical_hits = {n.id: 0 for n in nodes}
    for _ in range(iterations):
        sample = {n.id: _beta_pert_sample(rng, n.o, n.m, n.p) for n in nodes}
        sched = compute_schedule(nodes, sample)
        finishes.append(sched.duration)
        for a in sched.critical:
            critical_hits[a] += 1
    finishes.sort()

    def pct(q: float) -> float:
        return round(finishes[min(len(finishes) - 1, int(q * len(finishes)))], 3)

    return {
        "iterations": iterations, "seed": seed,
        "p50_days": pct(0.5), "p80_days": pct(0.8), "p90_days": pct(0.9),
        "mean_days": round(sum(finishes) / len(finishes), 3),
        "criticality_index": {k: round(v / iterations, 3) for k, v in critical_hits.items()},
    }
