"""N2 Product — PRD from extracted RFQ fields plus deterministic engineering checks.

Gate K: "Critical requirements traceable to tests" -> `traceability(prd, test_plan)`.
"""
from __future__ import annotations

from ..contracts.rfq import FIELDS, PRD_SCHEMA_VERSION, RFQ_FIELDS

CATEGORY = {
    "quantity": "volume", "supply_voltage": "electrical", "max_current": "electrical", "power_budget": "electrical",
    "operating_temp_min": "environmental", "operating_temp_max": "environmental", "board_length": "mechanical",
    "board_width": "mechanical", "interfaces": "interface", "certifications": "compliance",
    "target_unit_price": "commercial",
}
SELV_LIMIT_V = 60.0  # above ~60 V DC treat as hazardous voltage: safety review required


def _fmt(value) -> str:
    if isinstance(value, list):
        return ", ".join(value)
    if isinstance(value, dict) and "amount" in value:
        return f"{value['amount']} {value['currency']}"
    return str(value)


def engineering_checks(fields: dict, headroom: float = 0.8) -> list[dict]:
    """fields: extraction['fields'] ({name: {value, unit, ...}}). Returns findings with severity."""
    findings = []

    def add(severity, check, message):
        findings.append({"severity": severity, "check": check, "message": message})

    for name in ("quantity", "supply_voltage", "operating_temp_min", "operating_temp_max"):
        if name not in fields:
            add("error", "required_field", f"missing required field {name}")
    for name, f in fields.items():
        spec = FIELDS.get(name)
        if spec and spec.unit and f.get("unit") != spec.unit:
            add("error", "units", f"{name} must be in {spec.unit}, got {f.get('unit')}")
    v = fields.get("supply_voltage", {}).get("value")
    i = fields.get("max_current", {}).get("value")
    p = fields.get("power_budget", {}).get("value")
    if v is not None and i is not None:
        vi = round(v * i, 6)
        if p is not None:
            if vi > p:
                add("error", "power", f"V×I = {vi} W exceeds the {p} W power budget")
            elif vi > headroom * p:
                add("warning", "power_derating", f"V×I = {vi} W leaves under {round((1 - headroom) * 100)}% headroom on {p} W")
        else:
            add("info", "power", f"derived worst-case power V×I = {vi} W; no power budget stated")
    t_lo = fields.get("operating_temp_min", {}).get("value")
    t_hi = fields.get("operating_temp_max", {}).get("value")
    if t_lo is not None and t_hi is not None and t_lo >= t_hi:
        add("error", "temperature", "operating_temp_min must be below operating_temp_max")
    if t_hi is not None and t_hi > 85:
        add("warning", "temperature_derating", f"max ambient {t_hi} °C exceeds 85 °C: needs extended-range parts and derating")
    if t_lo is not None and t_lo < -40:
        add("warning", "temperature_derating", f"min ambient {t_lo} °C is below -40 °C: check component ratings")
    if v is not None and v > SELV_LIMIT_V:
        add("warning", "safety", f"{v} V exceeds {SELV_LIMIT_V} V DC: hazardous-voltage design, safety review required")
    return findings


def build_prd(extraction: dict) -> dict:
    fields = extraction.get("fields", {})
    requirements = []
    for spec in RFQ_FIELDS:
        if spec.name == "customer" or spec.name not in fields:
            continue
        f = fields[spec.name]
        requirements.append({
            "id": f"REQ-{len(requirements) + 1:03d}", "field": spec.name, "category": CATEGORY[spec.name],
            "statement": spec.statement.format(v=_fmt(f["value"])), "value": f["value"], "unit": f.get("unit"),
            "critical": spec.critical, "source_confidence": f.get("confidence"), "source": f.get("source"),
        })
    findings = engineering_checks(fields)
    return {
        "schema_version": PRD_SCHEMA_VERSION, "customer": fields.get("customer", {}).get("value"),
        "requirements": requirements, "findings": findings,
        "blocking": [f for f in findings if f["severity"] == "error"],
        "extraction_needs_review": extraction.get("needs_review", True),
    }


def traceability(prd: dict, test_plan: dict) -> dict:
    covered_by: dict[str, list[str]] = {}
    for t in test_plan.get("tests", []):
        for req in t.get("covers", []):
            covered_by.setdefault(req, []).append(t["id"])
    critical = [r["id"] for r in prd.get("requirements", []) if r.get("critical")]
    uncovered = [r for r in critical if not covered_by.get(r)]
    return {"critical_total": len(critical), "covered": len(critical) - len(uncovered), "uncovered": uncovered,
            "matrix": {r["id"]: covered_by.get(r["id"], []) for r in prd.get("requirements", [])},
            "passed": bool(critical) and not uncovered}
