"""Deterministic N0 validation engine. No LLM is involved in anything in this module.

Every check emits ``RuleViolation`` records from one catalog (rule id, error code, severity,
category, pipeline stage). The decision is a pure function of the violations:

    any BLOCK  → BLOCK     (hard policy: safety, IP/licensing, confidentiality, tenant isolation,
                            calibration, authorization, integrity, revision control, provenance)
    any REWORK → REWORK    (data defects the producer must fix)
    any REVIEW → REVIEW    (a human must look; only REVIEW can be resolved by an approval)
    otherwise  → PASS      (the declared type/stage checks passed — not product certification)

The numeric ``quality_score`` is advisory and is never consulted by ``decide``.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Optional

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .models import (CONFIDENTIALITY_RANK, PAYLOAD_MODELS, RELEASE_STAGE_ORDER, REQUIREMENT_LISTS, Artifact,
                     Derivation, Requirement, RightsDecision, RuleViolation, SourceRecord)
from .units import UnitError, normalize, parameter_dimension

# ----------------------------------------------------------------------------------------------
# rule catalog
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    code: str
    severity: str
    category: str
    stage: str
    description: str


_CATALOG = [
    # N0.1 intake & permissions
    ("N0-AUTH-001", "UNAUTHORIZED_PRODUCER", "BLOCK", "AUTHORIZATION", "N0.1", "producer lacks the role or node attribute for this submission"),
    ("N0-TEN-001", "CROSS_TENANT_REFERENCE", "BLOCK", "TENANT_ISOLATION", "N0.1", "artifact, source or upstream reference belongs to another tenant"),
    ("N0-RIGHTS-001", "SOURCE_NOT_REGISTERED", "BLOCK", "RIGHTS", "N0.1", "source_id has no SourceRecord.v1 in this tenant"),
    ("N0-RIGHTS-002", "SOURCE_NOT_APPROVED", "BLOCK", "RIGHTS", "N0.1", "SourceRecord.v1 is not APPROVED"),
    ("N0-RIGHTS-003", "PURPOSE_NOT_PERMITTED", "BLOCK", "RIGHTS", "N0.1", "processing_purpose is not in the source's permitted_use"),
    ("N0-RIGHTS-004", "LICENSE_RESTRICTED", "BLOCK", "IP_LICENSING", "N0.1", "license forbids this use"),
    ("N0-RIGHTS-005", "RIGHTS_EXPIRED", "BLOCK", "RIGHTS", "N0.1", "source rights have expired"),
    ("N0-RIGHTS-006", "SOURCE_MISMATCH", "BLOCK", "RIGHTS", "N0.1", "a referenced file was uploaded under a different source"),
    ("N0-CONF-001", "CONFIDENTIALITY_CLEARANCE", "BLOCK", "CONFIDENTIALITY", "N0.1", "data classification exceeds the principal's clearance"),
    ("N0-INT-001", "SHA256_MISMATCH", "BLOCK", "INTEGRITY", "N0.1", "declared SHA-256 does not match the stored bytes"),
    ("N0-INT-002", "BLOB_NOT_CLEARED", "BLOCK", "INTEGRITY", "N0.1", "referenced file is missing, rejected or not yet scanned"),
    ("N0-REV-001", "SUPERSEDED_REVISION", "BLOCK", "REVISION_CONTROL", "N0.1", "a newer revision of this artifact exists"),
    ("N0-REV-002", "REVISION_IMMUTABLE", "BLOCK", "REVISION_CONTROL", "N0.1", "this revision was already submitted with different content"),
    # N0.2 parsing & normalization
    ("N0-REV-003", "REVISION_FORMAT", "REWORK", "REVISION_CONTROL", "N0.2", "revision is not A..ZZZ or a number"),
    ("N0-PARSE-001", "PARSE_FAILED", "REWORK", "PARSING", "N0.2", "a referenced document could not be parsed"),
    ("N0-PII-001", "PII_DETECTED", "REVIEW", "CONFIDENTIALITY", "N0.2", "personal data found without PERSONAL_DATA_PROCESSING rights"),
    ("N0-UNIT-001", "UNIT_UNKNOWN", "REWORK", "UNITS", "N0.2", "unit is not in the SI registry"),
    ("N0-UNIT-002", "UNIT_DIMENSION_MISMATCH", "REWORK", "UNITS", "N0.2", "unit dimension disagrees with the parameter suffix"),
    ("N0-UNIT-003", "UNIT_MISSING", "REWORK", "UNITS", "N0.2", "numeric engineering value without a unit"),
    # N0.3 context assembly
    ("N0-CTX-001", "ARTIFACT_TYPE_MISMATCH", "REVIEW", "CLASSIFICATION", "N0.3", "payload shape does not match the declared artifact_type"),
    ("N0-CTX-002", "UPSTREAM_NOT_FOUND", "BLOCK", "LINEAGE", "N0.3", "upstream artifact reference does not resolve"),
    ("N0-CTX-003", "UPSTREAM_NOT_RELEASED", "BLOCK", "LINEAGE", "N0.3", "upstream artifact revision is not PASS"),
    ("N0-CTX-004", "UPSTREAM_SUPERSEDED", "BLOCK", "REVISION_CONTROL", "N0.3", "upstream reference pins a superseded revision"),
    ("N0-CTX-005", "EVIDENCE_NOT_FOUND", "REWORK", "PROVENANCE", "N0.3", "evidence snippet does not occur in the source document"),
    ("N0-CTX-006", "MISSING_CRITICAL_FIELD", "REWORK", "COMPLETENESS", "N0.3", "a critical field or specification is missing"),
    # N0.4 data quality & anomalies
    ("N0-SCH-001", "SCHEMA_INVALID", "REWORK", "SCHEMA", "N0.4", "payload violates its JSON Schema 2020-12 contract"),
    ("N0-PHY-001", "POWER_BALANCE_MISMATCH", "REWORK", "PHYSICS", "N0.4", "stated power disagrees with V × I"),
    ("N0-PHY-002", "EFFICIENCY_OUT_OF_RANGE", "REWORK", "PHYSICS", "N0.4", "efficiency outside (0, 100] % or output exceeds input"),
    ("N0-PHY-003", "RANGE_INVERTED", "REWORK", "PHYSICS", "N0.4", "a *_min value exceeds its *_max"),
    ("N0-PHY-004", "TOLERANCE_OUT_OF_BOUNDS", "REWORK", "PHYSICS", "N0.4", "tolerance negative or beyond 100 %"),
    ("N0-PHY-005", "DERATING_VIOLATION", "REWORK", "PHYSICS", "N0.4", "applied stress exceeds the derated rating"),
    ("N0-PHY-006", "NEGATIVE_MAGNITUDE", "REWORK", "PHYSICS", "N0.4", "a magnitude that cannot be negative is negative"),
    ("N0-DUP-001", "DUPLICATE_REQUIREMENT_ID", "REWORK", "DUPLICATES", "N0.4", "req_id used more than once"),
    ("N0-DUP-002", "DUPLICATE_CONTENT", "REVIEW", "DUPLICATES", "N0.4", "identical content already submitted under another artifact_id"),
    ("N0-FRESH-001", "STALE_DATA", "REWORK", "FRESHNESS", "N0.4", "data is outside its freshness window"),
    ("N0-CON-001", "CONTRADICTORY_VALUES", "REWORK", "CONSISTENCY", "N0.4", "the same parameter is stated with different values"),
    ("N0-PROV-001", "DERIVED_VALUE_MISMATCH", "REWORK", "PROVENANCE", "N0.4", "submitted DERIVED value disagrees with N0's computation"),
    ("N0-PROV-002", "DERIVATION_UNVERIFIABLE", "REWORK", "PROVENANCE", "N0.4", "DERIVED value N0 cannot recompute"),
    ("N0-PROV-003", "ORIGIN_MISLABELED", "BLOCK", "PROVENANCE", "N0.4", "value_origin misrepresents where a value came from"),
    ("N0-PROV-004", "EVIDENCE_MISSING", "REWORK", "PROVENANCE", "N0.4", "MODEL_EXTRACTED value without evidence snippets"),
    ("N0-PROV-005", "ORIGIN_INVALID_FOR_TYPE", "REWORK", "PROVENANCE", "N0.4", "value_origin not allowed for this artifact_type"),
    ("N0-PROV-006", "HUMAN_CONFIRMATION_REQUIRED", "REVIEW", "PROVENANCE", "N0.4", "model-extracted values need human confirmation"),
    ("N0-LLM-002", "MODEL_OUTPUT_SCHEMA_INVALID", "REWORK", "SCHEMA", "N0.4", "LLM output failed schema revalidation"),
    ("N0-SAM-001", "SAM_TOTAL_MISMATCH", "REWORK", "ARITHMETIC", "N0.4", "verified SAM total or an item's FX conversion disagrees with N0's recomputation"),
    ("N0-SAM-002", "UNVERIFIED_SPEND_AS_VERIFIED", "BLOCK", "SAM_INTEGRITY", "N0.5", "verified SAM includes synthetic, unattested or non-VERIFIED_BUDGET spend"),
    # N0.5 hard policy
    ("N0-SYN-001", "SYNTHETIC_AS_REAL", "BLOCK", "SYNTHETIC", "N0.5", "synthetic data presented as real"),
    ("N0-SYN-002", "SYNTHETIC_STAGE_GATE", "BLOCK", "SYNTHETIC", "N0.5", "synthetic data beyond CONCEPT stage"),
    ("N0-CAL-001", "CALIBRATION_EXPIRED", "BLOCK", "CALIBRATION", "N0.5", "instrument calibration expired before the measurement"),
    ("N0-SAFE-001", "SAFETY_REVIEW_REQUIRED", "REVIEW", "SAFETY", "N0.5", "hazardous voltage without a declared safety standard"),
    ("N0-STAGE-001", "STAGE_GATE_VIOLATION", "BLOCK", "STAGE_GATE", "N0.5", "artifact does not meet its release_stage gate"),
    ("N0-STAGE-002", "MODEL_OUTPUT_UNREVIEWED", "REVIEW", "STAGE_GATE", "N0.5", "model output needs human review before release"),
    ("N0-VEND-001", "VENDOR_TRANSMISSION_UNAPPROVED", "BLOCK", "AUTHORIZATION", "N0.5", "input not approved for transmission to this vendor"),
    ("N0-LLM-001", "LLM_TRANSMISSION_UNAPPROVED", "BLOCK", "AUTHORIZATION", "N0.1", "source rights do not allow sending this data to an external model"),
]
RULES: dict[str, RuleSpec] = {code: RuleSpec(rid, code, sev, cat, stage, desc)
                              for rid, code, sev, cat, stage, desc in _CATALOG}
assert len(RULES) == len(_CATALOG)
HARD_POLICY_CATEGORIES = {"AUTHORIZATION", "TENANT_ISOLATION", "RIGHTS", "IP_LICENSING", "CONFIDENTIALITY",
                          "INTEGRITY", "REVISION_CONTROL", "SYNTHETIC", "CALIBRATION", "SAFETY", "STAGE_GATE",
                          "SAM_INTEGRITY"}
_DIGEST = hashlib.sha256(json.dumps(_CATALOG).encode()).hexdigest()[:12]
VALIDATOR_VERSION = f"n0-validator/1.0.0+rules.{_DIGEST}"


class Findings:
    def __init__(self) -> None:
        self.items: list[RuleViolation] = []
        self.checks: list[str] = []

    def add(self, code: str, message: str, field: Optional[str] = None) -> None:
        spec = RULES[code]
        self.items.append(RuleViolation(rule_id=spec.rule_id, code=code, severity=spec.severity,
                                        category=spec.category, stage=spec.stage, message=message, field=field))

    def ran(self, name: str) -> None:
        self.checks.append(name)

    def extend(self, other: "Findings") -> None:
        self.items += other.items
        self.checks += other.checks

    @property
    def has_block(self) -> bool:
        return any(v.severity == "BLOCK" for v in self.items)

    @property
    def codes(self) -> list[str]:
        return sorted({v.code for v in self.items})


def decide(violations: Iterable[RuleViolation]) -> str:
    sev = {v.severity for v in violations}
    for d in ("BLOCK", "REWORK", "REVIEW"):
        if d in sev:
            return d
    return "PASS"


def quality_score(violations: Iterable[RuleViolation]) -> float:
    """Advisory 0–100. Deliberately not an input to ``decide``."""
    s = 100.0
    for v in violations:
        s -= {"BLOCK": 100, "REWORK": 25, "REVIEW": 10}[v.severity]
    return max(0.0, s)


def explain(decision: str, violations: list[RuleViolation], missing: list[str]) -> str:
    if decision == "PASS":
        return ("All deterministic checks for the declared artifact_type and release_stage passed. "
                "This is not product certification.")
    head = {"BLOCK": "Blocked by hard policy", "REWORK": "Returned for rework",
            "REVIEW": "Held for human review"}[decision]
    worst = [v for v in violations if v.severity == decision] or violations
    parts = [f"{v.rule_id} {v.code}: {v.message}" for v in worst[:6]]
    if missing:
        parts.append(f"missing: {', '.join(missing[:8])}")
    return f"{head}. " + " | ".join(parts)


# ----------------------------------------------------------------------------------------------
# hashing / canonical JSON
# ----------------------------------------------------------------------------------------------
def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_REV = re.compile(r"^(?:REV\.?\s*)?([A-Z]{1,3}|\d{1,6})$")


def normalize_revision(rev: str) -> Optional[str]:
    m = _REV.match(rev.strip().upper())
    return m.group(1) if m else None


def revision_order(rev: str) -> int:
    if rev.isdigit():
        return int(rev)
    n = 0
    for ch in rev:
        n = n * 26 + (ord(ch) - 64)
    return n


# ----------------------------------------------------------------------------------------------
# rights, licensing, confidentiality (N0.1)
# ----------------------------------------------------------------------------------------------
_PROHIBITED_LICENSES = {"PROHIBITED", "NONE", "LicenseRef-No-Use", "LicenseRef-All-Rights-Reserved-No-Use"}


def evaluate_rights(artifact_purpose: str, source_id: str, source: Optional[SourceRecord], now: datetime,
                    f: Findings) -> RightsDecision:
    f.ran("rights")
    reasons: list[str] = []
    if source is None:
        f.add("SOURCE_NOT_REGISTERED", f"source {source_id} is not registered for this tenant", "source_id")
        return RightsDecision(status="DENIED", source_id=source_id, processing_purpose=artifact_purpose,
                              purpose_permitted=False, llm_processing_allowed=False, vendor_transmission_allowed=False,
                              training_allowed=False, reasons=["source not registered"])
    approved = source.approval_status == "APPROVED"
    if not approved:
        f.add("SOURCE_NOT_APPROVED", f"source {source_id} is {source.approval_status}", "source_id")
        reasons.append(f"approval_status {source.approval_status}")
    if source.valid_until is not None and source.valid_until < now:
        f.add("RIGHTS_EXPIRED", f"rights for {source_id} expired {source.valid_until.isoformat()}", "source_id")
        reasons.append("rights expired")
    permitted = artifact_purpose in source.permitted_use
    if not permitted:
        f.add("PURPOSE_NOT_PERMITTED", f"{artifact_purpose} is not in permitted_use {source.permitted_use}",
              "processing_purpose")
        reasons.append("purpose not permitted")
    nc = "-NC" in source.license.upper()
    if source.license in _PROHIBITED_LICENSES or (nc and artifact_purpose != "EVALUATION"):
        f.add("LICENSE_RESTRICTED", f"license {source.license} does not allow {artifact_purpose}", "license")
        reasons.append("license restricted")
    ok = approved and not reasons
    return RightsDecision(
        status="GRANTED" if ok else "DENIED", source_id=source_id, approval_status=source.approval_status,
        license=source.license, processing_purpose=artifact_purpose, purpose_permitted=permitted,
        llm_processing_allowed=ok and "LLM_EXTRACTION" in source.permitted_use,
        vendor_transmission_allowed=ok and "VENDOR_TRANSMISSION" in source.permitted_use,
        training_allowed=ok and "MODEL_TRAINING" in source.permitted_use and not nc and source.data_origin == "REAL",
        reasons=reasons)


def check_clearance(classification: str, clearance: str, f: Findings) -> None:
    f.ran("confidentiality")
    if CONFIDENTIALITY_RANK[classification] > CONFIDENTIALITY_RANK[clearance]:
        f.add("CONFIDENTIALITY_CLEARANCE", f"{classification} data, principal cleared for {clearance}")


# ----------------------------------------------------------------------------------------------
# requirement normalization, derivation, physics (N0.2 / N0.4)
# ----------------------------------------------------------------------------------------------
PARAMETER_ALIASES = {"vout_v": "output_voltage_v", "iout_a": "output_current_a", "vin_v": "input_voltage_v",
                     "pout_w": "output_power_w", "eff_pct": "efficiency_pct", "tmin_c": "operating_temp_min_c",
                     "tmax_c": "operating_temp_max_c"}
_SIGNED_OK = {"voltage", "temperature", "ratio", None}


def normalize_requirements(reqs: list[Requirement], list_field: str, f: Findings) -> list[Requirement]:
    f.ran("units")
    out = []
    for i, r in enumerate(reqs):
        where = f"payload.{list_field}[{i}]"
        param = PARAMETER_ALIASES.get(r.parameter, r.parameter)
        si_value = si_unit = None
        numeric = isinstance(r.value, (int, float)) and not isinstance(r.value, bool)
        dim = parameter_dimension(param)
        if numeric:
            unit = r.unit
            if dim == "temperature" and unit is not None and unit.strip() == "C":
                unit = "degC"               # "C" on a *_c parameter means degrees Celsius, not coulomb
            if unit is None:
                if dim not in (None, "count"):
                    f.add("UNIT_MISSING", f"{param}={r.value} has no unit", where)
            else:
                try:
                    si_value, si_unit, udim = normalize(float(r.value), unit)
                    if dim and udim != dim:
                        f.add("UNIT_DIMENSION_MISMATCH", f"{param} is {dim} but unit {unit!r} is {udim}", where)
                        si_value = si_unit = None
                except UnitError as exc:
                    f.add("UNIT_UNKNOWN", str(exc), where)
        out.append(r.model_copy(update={"parameter": param, "si_value": si_value, "si_unit": si_unit}))
    return out


@dataclass(frozen=True)
class DerivationRule:
    parameter: str
    inputs: tuple[str, ...]
    fn: Callable[..., float]
    formula: str
    unit: str


DERIVATIONS = (
    DerivationRule("nominal_output_power_w", ("output_voltage_v", "output_current_a"), lambda v, i: v * i,
                   "nominal_output_power_w = output_voltage_v × output_current_a", "W"),
    DerivationRule("nominal_input_power_w", ("nominal_output_power_w", "efficiency_pct"), lambda p, e: p / e,
                   "nominal_input_power_w = nominal_output_power_w ÷ efficiency", "W"),
    DerivationRule("max_output_power_w", ("output_voltage_v", "output_current_max_a"), lambda v, i: v * i,
                   "max_output_power_w = output_voltage_v × output_current_max_a", "W"),
)
RESERVED_DERIVED = {d.parameter for d in DERIVATIONS}


def _single_values(reqs: Iterable[Requirement]) -> dict[str, tuple[float, str]]:
    """parameter → (si_value, req_id) for parameters stated exactly once (contradictions excluded)."""
    seen: dict[str, set] = {}
    first: dict[str, tuple[float, str]] = {}
    for r in reqs:
        if r.si_value is None or r.value_origin == "DERIVED":
            continue
        seen.setdefault(r.parameter, set()).add(round(r.si_value, 9))
        first.setdefault(r.parameter, (r.si_value, r.req_id))
    return {p: v for p, v in first.items() if len(seen[p]) == 1}


def derive_values(reqs: list[Requirement]) -> list[Requirement]:
    """Deterministic derived values, always tagged DERIVED with their formula and inputs."""
    known = _single_values(reqs)
    derived: list[Requirement] = []
    for rule in DERIVATIONS:
        if all(i in known for i in rule.inputs):
            try:
                value = rule.fn(*(known[i][0] for i in rule.inputs))
            except ZeroDivisionError:
                continue
            value = float(f"{value:.10g}")
            known[rule.parameter] = (value, f"DRV-{rule.parameter}")
            derived.append(Requirement(
                req_id=f"DRV-{rule.parameter}", parameter=rule.parameter, value=value, unit=rule.unit,
                value_origin="DERIVED", si_value=value, si_unit=rule.unit,
                derivation=Derivation(formula=rule.formula, inputs=[f"{i}:{known[i][1]}" for i in rule.inputs])))
    return derived


def _rel_tol(r: Requirement, default: float = 0.02) -> float:
    if r.tolerance is not None and r.tolerance.kind == "PCT":
        return max(r.tolerance.plus, r.tolerance.minus, 0.0) / 100.0 or default
    return default


def physics_checks(reqs: list[Requirement], derived: list[Requirement], list_field: str, f: Findings) -> None:
    f.ran("physics")
    by = {r.parameter: r for r in reqs if r.si_value is not None and r.value_origin != "DERIVED"}
    d = {r.parameter: r for r in derived}
    nominal = d.get("nominal_output_power_w")
    stated = by.get("output_power_w")
    if nominal and stated:
        tol = _rel_tol(stated)
        if abs(stated.si_value - nominal.si_value) > tol * max(abs(nominal.si_value), 1e-12):
            f.add("POWER_BALANCE_MISMATCH",
                  f"output_power_w={stated.si_value:g} W but output_voltage_v × output_current_a = "
                  f"{nominal.si_value:g} W (tolerance {tol * 100:g} %)", stated.req_id)
    eff = by.get("efficiency_pct")
    if eff is not None and not (0 < eff.si_value <= 1):
        f.add("EFFICIENCY_OUT_OF_RANGE", f"efficiency {eff.value} {eff.unit} is outside (0, 100] %", eff.req_id)
    pin = by.get("input_power_w")
    if pin is not None and nominal is not None and nominal.si_value > pin.si_value * (1 + _rel_tol(pin)):
        f.add("EFFICIENCY_OUT_OF_RANGE", f"output power {nominal.si_value:g} W exceeds input power "
                                         f"{pin.si_value:g} W (efficiency > 100 %)", pin.req_id)
    for r in reqs:
        m = re.match(r"^(.+)_min_([a-z]+)$", r.parameter)
        if m and r.si_value is not None:
            mx = by.get(f"{m.group(1)}_max_{m.group(2)}")
            if mx is not None and r.si_value > mx.si_value:
                f.add("RANGE_INVERTED", f"{r.parameter}={r.value} exceeds {mx.parameter}={mx.value}", r.req_id)
        if r.tolerance is not None:
            t = r.tolerance
            bad = t.plus < 0 or t.minus < 0 or (t.kind == "PCT" and (t.plus > 100 or t.minus > 100))
            if bad:
                f.add("TOLERANCE_OUT_OF_BOUNDS", f"{r.parameter} tolerance +{t.plus}/-{t.minus} {t.kind}", r.req_id)
        m = re.match(r"^(.+)_rated_([a-z]+)$", r.parameter)
        if m and r.si_value is not None:
            for kind in ("applied", "operating"):
                ap = by.get(f"{m.group(1)}_{kind}_{m.group(2)}")
                if ap is not None and abs(ap.si_value) > 0.8 * abs(r.si_value):
                    f.add("DERATING_VIOLATION", f"{ap.parameter}={ap.value} exceeds 80 % of "
                                                f"{r.parameter}={r.value}", ap.req_id)
        dim = parameter_dimension(r.parameter)
        if r.si_value is not None and r.si_value < 0 and dim not in _SIGNED_OK:
            f.add("NEGATIVE_MAGNITUDE", f"{r.parameter}={r.value} cannot be negative", r.req_id)


def consistency_checks(reqs: list[Requirement], list_field: str, f: Findings) -> None:
    f.ran("duplicates")
    f.ran("contradictions")
    ids: dict[str, int] = {}
    for r in reqs:
        ids[r.req_id] = ids.get(r.req_id, 0) + 1
    for rid, n in ids.items():
        if n > 1:
            f.add("DUPLICATE_REQUIREMENT_ID", f"req_id {rid} appears {n} times", f"payload.{list_field}")
    groups: dict[tuple, set] = {}
    for r in reqs:
        if r.value_origin == "DERIVED" or r.value is None:
            continue
        key = (r.parameter, json.dumps(r.operating_conditions, sort_keys=True, default=str))
        groups.setdefault(key, set()).add(round(r.si_value, 9) if r.si_value is not None else str(r.value))
    for (param, cond), values in groups.items():
        if len(values) > 1:
            f.add("CONTRADICTORY_VALUES", f"{param} is stated as {sorted(map(str, values))}"
                                          + (f" under {cond}" if cond != "{}" else ""), f"payload.{list_field}")


def provenance_checks(artifact_type: str, payload: dict, reqs: list[Requirement], derived: list[Requirement],
                      f: Findings) -> None:
    """Value attribution: CUSTOMER vs MEASURED vs DERIVED vs MODEL_EXTRACTED must be truthful."""
    f.ran("provenance")
    computed = {r.parameter: r for r in derived}
    from_model = bool(payload.get("extraction_model_run_id")) or artifact_type == "MODEL_OUTPUT"
    extracted = 0
    for r in reqs:
        if r.parameter in RESERVED_DERIVED and r.value_origin != "DERIVED":
            f.add("ORIGIN_MISLABELED", f"{r.parameter} is computed by N0 from other values; it cannot be "
                                       f"{r.value_origin}", r.req_id)
        if r.value_origin == "DERIVED":
            if r.parameter not in RESERVED_DERIVED:
                f.add("DERIVATION_UNVERIFIABLE", f"N0 has no deterministic derivation for {r.parameter}", r.req_id)
            elif r.parameter not in computed:
                f.add("DERIVATION_UNVERIFIABLE", f"inputs for {r.parameter} are missing or contradictory", r.req_id)
            elif r.si_value is not None and abs(r.si_value - computed[r.parameter].si_value) > \
                    0.005 * max(abs(computed[r.parameter].si_value), 1e-12):
                f.add("DERIVED_VALUE_MISMATCH", f"{r.parameter}={r.si_value:g} but N0 computes "
                                                f"{computed[r.parameter].si_value:g}", r.req_id)
        if r.value_origin == "MEASURED" and artifact_type not in ("TEST_MEASUREMENT", "TELEMETRY"):
            f.add("ORIGIN_INVALID_FOR_TYPE", f"MEASURED values belong in TEST_MEASUREMENT/TELEMETRY, not "
                                             f"{artifact_type}", r.req_id)
        if artifact_type in ("TEST_MEASUREMENT", "TELEMETRY") and r.value_origin not in ("MEASURED", "DERIVED"):
            f.add("ORIGIN_INVALID_FOR_TYPE", f"{artifact_type} values must be MEASURED (got {r.value_origin})",
                  r.req_id)
        if from_model and r.value_origin in ("CUSTOMER", "MEASURED"):
            f.add("ORIGIN_MISLABELED", f"{r.parameter} came from a model run but is labelled {r.value_origin}; "
                                       "confirm it through /v1/gateway/feedback and submit a new revision", r.req_id)
        if r.value_origin == "MODEL_EXTRACTED":
            extracted += 1
            if r.value is not None and not r.evidence_snippets:
                f.add("EVIDENCE_MISSING", f"{r.parameter} was model-extracted without an evidence snippet", r.req_id)
    if extracted:
        f.add("HUMAN_CONFIRMATION_REQUIRED", f"{extracted} model-extracted value(s) await human confirmation")


# ----------------------------------------------------------------------------------------------
# schema, completeness, classification, freshness (N0.3 / N0.4)
# ----------------------------------------------------------------------------------------------
_VALIDATORS = {t: Draft202012Validator(m.model_json_schema()) for t, m in PAYLOAD_MODELS.items()}
POWER_CATEGORIES = {"power_supply", "dc_dc_converter", "ac_dc_converter", "battery_charger", "led_driver",
                    "motor_driver", "inverter"}
CRITICAL_SPECS = {   # alternatives: any one tuple fully present satisfies the requirement
    "input voltage": [("input_voltage_v",), ("input_voltage_min_v", "input_voltage_max_v")],
    "output voltage": [("output_voltage_v",)],
    "output current": [("output_current_a",), ("output_current_max_a",)],
    "operating temperature range": [("operating_temp_min_c", "operating_temp_max_c")],
}


def schema_checks(artifact: Artifact, f: Findings) -> tuple[Optional[Any], list[str]]:
    """JSON Schema 2020-12 on the payload; ``required`` errors become missing fields, not schema errors."""
    f.ran("json_schema_2020_12")
    missing: list[str] = []
    errors = sorted(_VALIDATORS[artifact.artifact_type].iter_errors(artifact.payload), key=lambda e: list(e.path))
    for e in errors:
        where = "payload" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in e.absolute_path)
        if e.validator == "required":
            for prop in e.validator_value:
                if isinstance(e.instance, dict) and prop not in e.instance:
                    missing.append(f"{where}.{prop}")
        else:
            f.add("SCHEMA_INVALID", e.message[:300], where)
    model = None
    if not errors:
        try:
            model = PAYLOAD_MODELS[artifact.artifact_type].model_validate(artifact.payload)
        except ValidationError as exc:
            for err in exc.errors(include_url=False)[:10]:
                f.add("SCHEMA_INVALID", err["msg"], "payload." + ".".join(map(str, err["loc"])))
    return model, missing


def missing_specs(artifact_type: str, payload: dict, reqs: list[Requirement]) -> list[str]:
    if artifact_type != "RFQ" or str(payload.get("product_category", "")).lower() not in POWER_CATEGORIES:
        return []
    present = {r.parameter for r in reqs if r.value is not None}
    out = []
    for name, alternatives in CRITICAL_SPECS.items():
        if not any(all(p in present for p in alt) for alt in alternatives):
            out.append(f"payload.requirements[parameter={'|'.join('+'.join(a) for a in alternatives)}]")
    return out


_SIGNATURES = [("RFQ", {"rfq_id", "requirements"}), ("DATASHEET", {"part_number", "manufacturer"}),
               ("EDA_PACKAGE", {"design_id", "eda_tool"}), ("SUPPLIER_QUOTE", {"quote_id", "supplier_id"}),
               ("TEST_MEASUREMENT", {"test_id", "instruments"}), ("MARKET_FEED", {"feed_id", "series"}),
               ("FINANCE_RECORD", {"record_id", "period"}), ("TELEMETRY", {"device_id", "readings"}),
               ("MODEL_OUTPUT", {"model_run_id", "task"}), ("MARKET_BATCH", {"batch_id", "observations"}),
               ("BUYER_RECORD", {"batch_id", "records"}), ("COMMERCIAL_SHORTLIST", {"report_id", "entries"}),
               ("SAM_EVIDENCE", {"report_id", "verified_items"})]


def classify(payload: dict) -> Optional[str]:
    keys = set(payload)
    return next((t for t, sig in _SIGNATURES if sig <= keys), None)


def _as_dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


FRESHNESS = {"MARKET_FEED": ("as_of", timedelta(hours=24)), "TELEMETRY": ("observed_at", timedelta(hours=1)),
             "MARKET_BATCH": ("retrieved_at", timedelta(days=30)), "BUYER_RECORD": ("retrieved_at", timedelta(days=30))}


def freshness_checks(artifact_type: str, payload: dict, now: datetime, f: Findings) -> None:
    f.ran("freshness")
    if artifact_type == "SUPPLIER_QUOTE":
        vu = _as_dt(payload.get("valid_until"))
        if vu is not None and vu.tzinfo is not None and vu < now:
            f.add("STALE_DATA", f"quote expired {vu.isoformat()}", "payload.valid_until")
    if artifact_type in FRESHNESS:
        key, window = FRESHNESS[artifact_type]
        t = _as_dt(payload.get(key))
        if t is not None and t.tzinfo is not None:
            if now - t > window:
                f.add("STALE_DATA", f"{key} {t.isoformat()} is older than {window}", f"payload.{key}")
            elif t - now > timedelta(minutes=5):
                f.add("STALE_DATA", f"{key} {t.isoformat()} is in the future", f"payload.{key}")
    if artifact_type == "TEST_MEASUREMENT":
        t = _as_dt(payload.get("measured_at"))
        if t is not None and t.tzinfo is not None and t - now > timedelta(minutes=5):
            f.add("STALE_DATA", "measured_at is in the future", "payload.measured_at")


# ----------------------------------------------------------------------------------------------
# hard policy (N0.5)
# ----------------------------------------------------------------------------------------------
def _walk_keys(obj: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def hard_policy_checks(artifact: Artifact, source: Optional[SourceRecord], reqs: list[Requirement],
                       now: datetime, f: Findings) -> None:
    f.ran("synthetic")
    f.ran("stage_gate")
    f.ran("calibration")
    f.ran("safety")
    p, stage = artifact.payload, RELEASE_STAGE_ORDER[artifact.release_stage]
    # a nested ``synthetic: true`` (e.g. one market observation in a batch) taints the whole artifact
    marked = any(k in ("_synthetic", "synthetic") and v is True for k, v in _walk_keys(p))
    if not artifact.synthetic and ((source is not None and source.data_origin == "SYNTHETIC") or marked):
        f.add("SYNTHETIC_AS_REAL", "synthetic data (source data_origin SYNTHETIC or a synthetic marker in the "
                                   "payload) submitted with synthetic=false", "synthetic")
    if artifact.synthetic and stage > RELEASE_STAGE_ORDER["CONCEPT"]:
        f.add("SYNTHETIC_STAGE_GATE", f"synthetic data cannot enter release_stage {artifact.release_stage}",
              "release_stage")
    if artifact.artifact_type == "TEST_MEASUREMENT":
        measured = _as_dt(p.get("measured_at"))
        for i, inst in enumerate(p.get("instruments") or []):
            due = _as_dt(inst.get("calibration_due")) if isinstance(inst, dict) else None
            if measured and due and due < measured:
                f.add("CALIBRATION_EXPIRED", f"{inst.get('instrument_id')} calibration due {due.date()} before "
                                             f"measurement {measured.date()}", f"payload.instruments[{i}]")
    if artifact.artifact_type == "EDA_PACKAGE" and stage >= RELEASE_STAGE_ORDER["PILOT"]:
        errs = int(p.get("drc_errors") or 0) + int(p.get("erc_errors") or 0)
        if errs:
            f.add("STAGE_GATE_VIOLATION", f"{errs} open DRC/ERC errors at {artifact.release_stage}", "payload")
    if artifact.artifact_type == "MODEL_OUTPUT":
        if stage >= RELEASE_STAGE_ORDER["PILOT"]:
            f.add("STAGE_GATE_VIOLATION", f"model output cannot be released at {artifact.release_stage}",
                  "release_stage")
        else:
            f.add("MODEL_OUTPUT_UNREVIEWED", "model output requires human review before downstream use")
    if artifact.artifact_type in ("RFQ", "DATASHEET"):
        hazardous = [r for r in reqs if parameter_dimension(r.parameter) == "voltage" and r.si_value is not None
                     and abs(r.si_value) > 60]
        if hazardous and not p.get("compliance_standards"):
            f.add("SAFETY_REVIEW_REQUIRED", f"{hazardous[0].parameter}={hazardous[0].value} {hazardous[0].unit} "
                                            "exceeds the 60 V DC SELV limit and no safety standard is declared")


def _dec(v: Any) -> Optional[Decimal]:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def market_checks(artifact_type: str, payload: dict, synthetic: bool, f: Findings) -> None:
    """SAM_EVIDENCE integrity, recomputed deterministically: N0 never trusts a submitted total."""
    if artifact_type != "SAM_EVIDENCE":
        return
    f.ran("sam_integrity")
    items = payload.get("verified_items") or []
    total = Decimal(0)
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        where = f"payload.verified_items[{i}]"
        if it.get("synthetic") is True or it.get("spend_type") != "VERIFIED_BUDGET" or not it.get("attestation_id"):
            f.add("UNVERIFIED_SPEND_AS_VERIFIED", f"{it.get('evidence_id')} is synthetic, unattested or "
                                                  f"{it.get('spend_type')}; it cannot be verified SAM", where)
        amount = _dec((it.get("original") or {}).get("amount"))
        rate, base = _dec(it.get("fx_rate")), _dec(it.get("amount_base"))
        if amount is not None and rate is not None and base is not None:
            if abs(amount * rate - base) > Decimal("0.01"):
                f.add("SAM_TOTAL_MISMATCH", f"{it.get('evidence_id')}: {amount} × {rate} ≠ {base}", where)
            total += base
    stated = _dec(payload.get("verified_real_sam"))
    if stated is not None and abs(stated - total) > Decimal("0.01"):
        f.add("SAM_TOTAL_MISMATCH", f"verified_real_sam {stated} ≠ Σ verified_items {total}",
              "payload.verified_real_sam")
    if synthetic and stated is not None and stated > 0:
        f.add("UNVERIFIED_SPEND_AS_VERIFIED", "a synthetic artifact cannot carry verified real SAM",
              "payload.verified_real_sam")


# ----------------------------------------------------------------------------------------------
# routing (N0.6)
# ----------------------------------------------------------------------------------------------
TYPE_DESTINATIONS = {
    "RFQ": ["N1", "N2", "N6"], "DATASHEET": ["N2", "N3", "N5"], "EDA_PACKAGE": ["N3", "N4", "N5"],
    "SUPPLIER_QUOTE": ["N5", "N7"], "TEST_MEASUREMENT": ["N3", "N4", "N8"], "MARKET_FEED": ["N1", "N6", "N7"],
    "FINANCE_RECORD": ["N7"], "TELEMETRY": ["N4", "N8"], "MODEL_OUTPUT": ["N2", "N3"],
    "MARKET_BATCH": ["N1"], "BUYER_RECORD": ["N1"], "COMMERCIAL_SHORTLIST": ["N6"], "SAM_EVIDENCE": ["N7"],
}
PURPOSE_DESTINATIONS = {
    "RFQ_ANALYSIS": ["N1", "N2", "N6"], "PRODUCT_DEFINITION": ["N2", "N3"], "DESIGN_ENGINEERING": ["N3", "N4", "N5"],
    "VERIFICATION": ["N3", "N4", "N8"], "SOURCING": ["N5", "N7"], "COMMERCIAL": ["N6", "N7"], "FINANCE": ["N7"],
    "LIFECYCLE": ["N4", "N8"], "MARKET_INTELLIGENCE": ["N1", "N6", "N7"], "EVALUATION": [],
}


def allowed_destinations(decision: str, artifact_type: str, purpose: str, origin_node: str,
                         requested: Optional[list[str]]) -> list[str]:
    if decision != "PASS":
        return []
    nodes = [n for n in TYPE_DESTINATIONS[artifact_type] if n in PURPOSE_DESTINATIONS.get(purpose, [])
             and n != origin_node]
    if requested is not None:
        nodes = [n for n in nodes if n in requested]
    return nodes


# ----------------------------------------------------------------------------------------------
# LLM output revalidation
# ----------------------------------------------------------------------------------------------
def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def evidence_checks(reqs: list[Requirement], document_text: Optional[str], f: Findings) -> None:
    """Every MODEL_EXTRACTED value must quote text that really occurs in the parsed source."""
    f.ran("evidence")
    if document_text is None:
        return
    hay = _norm_text(document_text)
    for r in reqs:
        if r.value_origin != "MODEL_EXTRACTED":
            continue
        for s in r.evidence_snippets:
            if _norm_text(s.text) not in hay:
                f.add("EVIDENCE_NOT_FOUND", f"evidence for {r.parameter} ({s.text[:80]!r}) is not in the source "
                                            "document", r.req_id)
