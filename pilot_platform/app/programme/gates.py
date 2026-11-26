"""Acceptance-gate evaluation.

A gate spec (stored per activity in seed_plan.json) has two parts:

    {"evidence":  [{"field": "buyer_interviews", "op": "min", "value": 10}, ...],
     "automated": ["extraction_accuracy", ...]}

Evidence rules check the structured evidence a submitter attaches. Automated
checks are functions in `checks.py` that inspect live platform state (lineage,
eval accuracy, isolation probes, ...). A gate can only be passed when every
rule and every automated check passes; a human reviewer still makes the call.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: Any = None


@dataclass
class CheckContext:
    db: Session
    tenant_id: str
    settings: Any
    app: Any = None


CheckFn = Callable[[CheckContext], CheckResult]


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def evaluate_rule(rule: dict, evidence: dict) -> CheckResult:
    field, op, target = rule["field"], rule["op"], rule.get("value")
    name = f"evidence.{field} {op}" + (f" {target}" if target is not None else "")
    if field not in evidence:
        return CheckResult(name, False, "missing")
    v = evidence[field]
    if op == "min":
        ok = _is_number(v) and v >= target
    elif op == "max":
        ok = _is_number(v) and v <= target
    elif op == "eq":
        ok = v == target and type(v) is not bool
    elif op == "true":
        ok = v is True
    elif op == "nonempty":
        ok = v is not None and (len(v) > 0 if hasattr(v, "__len__") else True)
    elif op == "number":
        ok = _is_number(v)
    elif op == "in":
        ok = v in target
    else:
        return CheckResult(name, False, f"unknown operator {op!r}")
    return CheckResult(name, bool(ok), {"value": v})


def evaluate_gate(gate_spec: dict, evidence: dict, ctx: CheckContext) -> dict:
    from .checks import CHECKS

    results = [evaluate_rule(r, evidence) for r in gate_spec.get("evidence", [])]
    for check_name in gate_spec.get("automated", []):
        fn = CHECKS.get(check_name)
        if fn is None:
            results.append(CheckResult(f"auto.{check_name}", False, "unknown automated check"))
            continue
        try:
            res = fn(ctx)
            res.name = f"auto.{check_name}"
        except Exception as exc:  # a crashing check must fail closed, not open
            res = CheckResult(f"auto.{check_name}", False, f"check raised {type(exc).__name__}: {exc}")
        results.append(res)
    return {"passed": all(r.passed for r in results), "results": [asdict(r) for r in results]}
