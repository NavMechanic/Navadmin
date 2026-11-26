"""N4 Quality — PFMEA, test plan and verification matrix (gate N: "Safety-critical gaps block release").

Blocking gaps:
  * a critical requirement with no covering test
  * a PFMEA row with severity >= 9 and no recommended action
  * a PFMEA row with RPN (S×O×D) >= threshold and no recommended action
"""
from __future__ import annotations

from ..core.errors import ValidationFailed
from .n2_product import traceability

METHOD = {"electrical": "bench measurement", "environmental": "thermal chamber cycling",
          "mechanical": "dimensional inspection", "interface": "functional interface test",
          "compliance": "pre-compliance test"}
FIELD_LIMITS = {"max_current": "max", "power_budget": "max", "supply_voltage": "nominal",
                "operating_temp_min": "min", "operating_temp_max": "max", "board_length": "max", "board_width": "max"}


def pfmea(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows):
        for k in ("severity", "occurrence", "detection"):
            v = r.get(k)
            if not isinstance(v, int) or not 1 <= v <= 10:
                raise ValidationFailed(f"pfmea[{i}].{k} must be an integer 1-10")
        out.append({**r, "id": r.get("id", f"FM-{i + 1:03d}"), "rpn": r["severity"] * r["occurrence"] * r["detection"]})
    return out


def build_test_plan(prd: dict, tests: list[dict] | None = None) -> dict:
    """Use supplied tests, or derive one test per testable requirement."""
    if tests is not None:
        return {"tests": tests, "generated": False}
    generated = []
    for req in prd.get("requirements", []):
        method = METHOD.get(req["category"])
        if not method:
            continue  # volume/commercial requirements are verified commercially, not by test
        limit = {FIELD_LIMITS[req["field"]]: req["value"], "unit": req.get("unit")} if req["field"] in FIELD_LIMITS else {}
        generated.append({"id": f"TST-{len(generated) + 1:03d}", "name": f"Verify {req['field']}", "method": method,
                          "covers": [req["id"]], "limits": limit, "critical": req["critical"]})
    return {"tests": generated, "generated": True}


def quality_gate(prd: dict, test_plan: dict, pfmea_rows: list[dict], rpn_threshold: int) -> dict:
    trace = traceability(prd, test_plan)
    gaps = [f"critical requirement {r} has no covering test" for r in trace["uncovered"]]
    for row in pfmea_rows:
        if row.get("action"):
            continue
        if row["severity"] >= 9:
            gaps.append(f"{row['id']} severity {row['severity']} (safety-critical) has no action")
        elif row["rpn"] >= rpn_threshold:
            gaps.append(f"{row['id']} RPN {row['rpn']} >= {rpn_threshold} has no action")
    return {"verification_matrix": trace["matrix"], "traceability": trace, "blocking_gaps": gaps,
            "passed": not gaps and trace["passed"]}
