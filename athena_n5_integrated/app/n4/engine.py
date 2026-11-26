"""Deterministic N4 policy engine: traceability, PFMEA prioritisation and the verification decision.

No model output, score or optimizer is consulted by ``assess``. Decision precedence is BLOCK > REWORK >
PASS, and these always BLOCK: FAIL and INDETERMINATE verdicts, expired / missing / mismatched
calibration, a missing or unapproved test SOP revision, unmapped safety-critical requirements,
synthetic or mock evidence offered as qualification, wrong PCB revision, tampered evidence,
unadjudicated or confirmed AOI defects, and open safety-severity PFMEA actions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from ..models import METHOD_PROVENANCE, PHYSICAL_PROVENANCE, AttributeInspection, VerifiedMeasurement
from ..repository import new_id
from ..units import parameter_dimension
from . import HEURISTIC_LABEL
from .metrology import MetrologyError, calibration_problems, convert, judge, unit_info
from .models import (AcceptanceLimit, AoiDetection, N4Adjudication, N4CalibrationRecord,
                     N4Capa, N4DesignIntake, N4EvidenceIntake, N4Finding, N4Instrument, N4Measurement, N4Pfmea,
                     N4PfmeaEntry, N4TestMethod, N4TestPlan, N4TraceabilityMatrix, PfmeaEntryInput, TestCaseSpec,
                     TestVerdict, TraceRow)

ENGINE_VERSION = "n4-engine/1.0.0"
SAFETY_SEVERITY = 9

#: code → (outcome, category)
RULES: dict[str, tuple[str, str]] = {
    "MEASUREMENT_FAIL": ("BLOCK", "METROLOGY"), "GUARD_BAND_INDETERMINATE": ("BLOCK", "METROLOGY"),
    "UNIT_NOT_SI": ("BLOCK", "METROLOGY"), "DIMENSION_MISMATCH": ("BLOCK", "METROLOGY"),
    "UNCERTAINTY_MISSING": ("BLOCK", "METROLOGY"), "CAPTURE_IN_FUTURE": ("BLOCK", "METROLOGY"),
    "CALIBRATION_EXPIRED": ("BLOCK", "CALIBRATION"), "CALIBRATION_MISSING": ("BLOCK", "CALIBRATION"),
    "CALIBRATION_MISMATCH": ("BLOCK", "CALIBRATION"), "CALIBRATION_REVOKED": ("BLOCK", "CALIBRATION"),
    "CALIBRATION_NOT_YET_VALID": ("BLOCK", "CALIBRATION"), "INSTRUMENT_UNREGISTERED": ("BLOCK", "CALIBRATION"),
    "METHOD_SOP_MISSING": ("BLOCK", "METHOD"), "METHOD_REVISION_MISMATCH": ("BLOCK", "METHOD"),
    "UNMAPPED_SAFETY_CRITICAL": ("BLOCK", "SAFETY"), "SAFETY_REQUIRES_PHYSICAL": ("BLOCK", "SAFETY"),
    "NOT_TESTED_SAFETY": ("BLOCK", "SAFETY"), "SAMPLE_SIZE_SHORT_SAFETY": ("BLOCK", "SAFETY"),
    "TEST_CASE_INCOMPLETE_SAFETY": ("BLOCK", "SAFETY"),
    "SYNTHETIC_NOT_QUALIFICATION": ("BLOCK", "SYNTHETIC"), "PROVENANCE_METHOD_MISMATCH": ("BLOCK", "VERIFICATION"),
    "EVIDENCE_HASH_MISMATCH": ("BLOCK", "INTEGRITY"), "WRONG_PCB_REVISION": ("BLOCK", "REVISION_CONTROL"),
    "LIMIT_LOOSER_THAN_REQUIREMENT": ("BLOCK", "SAFETY"),
    "AOI_UNADJUDICATED": ("BLOCK", "INSPECTION"), "INSPECTION_DEFECT": ("BLOCK", "INSPECTION"),
    "PFMEA_SAFETY_DETECTION_UNMAPPED": ("BLOCK", "PFMEA"), "PFMEA_SAFETY_ACTION_OPEN": ("BLOCK", "PFMEA"),
    "ACTION_VERIFICATION_UNSUPPORTED": ("BLOCK", "PFMEA"),
    "NOT_TESTED": ("REWORK", "COVERAGE"), "SAMPLE_SIZE_SHORT": ("REWORK", "COVERAGE"),
    "TEST_CASE_INCOMPLETE": ("REWORK", "COVERAGE"), "REQUIREMENT_NOT_COVERED": ("REWORK", "COVERAGE"),
    "UNKNOWN_REQUIREMENT": ("REWORK", "COVERAGE"), "PFMEA_MISSING": ("REWORK", "PFMEA"),
    "PFMEA_HAZARD_UNADDRESSED": ("REWORK", "PFMEA"),
    "LOW_TUR": ("INFO", "METROLOGY"), "PFMEA_HIGH_ATTENTION": ("INFO", "PFMEA"),
}


class Findings:
    def __init__(self) -> None:
        self.items: list[N4Finding] = []

    def add(self, code: str, message: str, test_id: Optional[str] = None, requirement_id: Optional[str] = None,
            evidence_ids: Iterable[str] = ()) -> None:
        outcome, cat = RULES[code]
        key = (code, message, test_id, requirement_id)
        if any((f.code, f.message, f.test_id, f.requirement_id) == key for f in self.items):
            return
        self.items.append(N4Finding(finding_id=new_id("N4F"), code=code, outcome=outcome, category=cat,
                                    message=message, test_id=test_id, requirement_id=requirement_id,
                                    evidence_ids=sorted(set(evidence_ids))))


def decide(findings: Iterable[N4Finding]) -> str:
    outcomes = {f.outcome for f in findings}
    return "BLOCK" if "BLOCK" in outcomes else "REWORK" if "REWORK" in outcomes else "PASS"


# ----------------------------------------------------------------------------------------------
# N4.2 traceability
# ----------------------------------------------------------------------------------------------
def safety_critical_ids(intake: N4DesignIntake) -> set[str]:
    return {r.req_id for r in intake.requirements if r.safety_critical}


def traceability(plan: N4TestPlan, intake: N4DesignIntake) -> N4TraceabilityMatrix:
    known = {r.req_id: r for r in intake.requirements}
    unknown = sorted({rid for tc in plan.test_cases for rid in tc.requirement_ids if rid not in known})
    rows = []
    for r in intake.requirements:
        tcs = [tc for tc in plan.test_cases if r.req_id in tc.requirement_ids]
        methods = sorted({tc.method for tc in tcs})
        gaps = []
        if not tcs:
            gaps.append("NO_TEST")
        elif r.safety_critical and set(methods) <= {"SIM"}:
            gaps.append("NO_PHYSICAL_METHOD")
        if any(tc.owner is None for tc in tcs):
            gaps.append("OWNER_MISSING")
        if any(tc.method_ref is None for tc in tcs):
            gaps.append("METHOD_REF_MISSING")
        rows.append(TraceRow(requirement_id=r.req_id, parameter=r.parameter, safety_critical=r.safety_critical,
                             hazard_ids=r.hazard_ids, test_ids=[tc.test_id for tc in tcs], methods=methods,
                             owners=sorted({tc.owner for tc in tcs if tc.owner}), gaps=gaps))
    sc = [row for row in rows if row.safety_critical]
    covered = [row for row in sc if not ({"NO_TEST", "NO_PHYSICAL_METHOD"} & set(row.gaps))]
    pct = Decimal(100) if not sc else (Decimal(len(covered)) * 100 / Decimal(len(sc))).quantize(Decimal("0.1"))
    return N4TraceabilityMatrix(
        plan_id=plan.plan_id, rows=rows, safety_critical_total=len(sc), safety_critical_covered=len(covered),
        safety_critical_coverage_pct=str(pct),
        uncovered_safety_critical=[row.requirement_id for row in sc if row not in covered],
        uncovered_other=[row.requirement_id for row in rows if not row.safety_critical and "NO_TEST" in row.gaps
                         and known[row.requirement_id].value_origin != "DERIVED"],
        unknown_requirement_refs=unknown,
        complete=len(covered) == len(sc) and not unknown and not any(row.gaps for row in sc))


# ----------------------------------------------------------------------------------------------
# N4.3 PFMEA prioritisation (custom heuristic; a licensed AP table only when configured)
# ----------------------------------------------------------------------------------------------
@dataclass
class ApTable:
    license_ref: str
    table: dict

    @classmethod
    def load(cls, path: Optional[str], license_ref: Optional[str]) -> Optional["ApTable"]:
        if not path or not license_ref:
            return None
        return cls(license_ref, json.loads(Path(path).read_text()))

    def lookup(self, s: int, o: int, d: int) -> str:
        return self.table[str(s)][str(o)][str(d)]


def attention(s: int, o: int, d: int) -> tuple[str, str]:
    """The custom attention heuristic. It is not AIAG-VDA Action Priority."""
    if s >= SAFETY_SEVERITY:
        return "HIGH", f"severity {s} ≥ {SAFETY_SEVERITY} (safety / regulatory effect)"
    if s >= 7 and o >= 4:
        return "HIGH", f"severity {s} ≥ 7 with occurrence {o} ≥ 4"
    if s >= 5 and o >= 5 and d >= 6:
        return "HIGH", f"severity {s}, occurrence {o} and detection {d} all elevated"
    if s >= 5 and (o >= 3 or d >= 5):
        return "MEDIUM", f"severity {s} ≥ 5 with occurrence {o} ≥ 3 or detection {d} ≥ 5"
    return "LOW", "below the heuristic attention thresholds"


def rate_entry(e: PfmeaEntryInput, rated_by: str, ap: Optional[ApTable]) -> N4PfmeaEntry:
    level, basis = attention(e.severity, e.occurrence, e.detection)
    return N4PfmeaEntry(**e.model_dump(), rated_by=rated_by, rpn_illustrative=e.severity * e.occurrence * e.detection,
                        attention_level=level, attention_basis=f"{basis}. {HEURISTIC_LABEL}",
                        action_priority=ap.lookup(e.severity, e.occurrence, e.detection) if ap else None)


# ----------------------------------------------------------------------------------------------
# N4.5 verification
# ----------------------------------------------------------------------------------------------
@dataclass
class Registry:
    methods: dict[tuple[str, str], N4TestMethod] = field(default_factory=dict)
    instruments: dict[str, N4Instrument] = field(default_factory=dict)
    calibrations: dict[str, N4CalibrationRecord] = field(default_factory=dict)


@dataclass
class Evidence:
    intakes: list[N4EvidenceIntake]
    measurements: list[N4Measurement]
    adjudications: list[N4Adjudication]
    tampered: dict[str, str]                      # evidence_id → problem
    rejected: list[N4EvidenceIntake] = field(default_factory=list)


#: rejected-intake codes that keep blocking until a later accepted submission covers the same tests
HARD_REJECTIONS = {"WRONG_PCB_REVISION": "WRONG_PCB_REVISION", "SYNTHETIC_AS_PHYSICAL": "SYNTHETIC_NOT_QUALIFICATION",
                   "SHA256_MISMATCH": "EVIDENCE_HASH_MISMATCH", "MALWARE_DETECTED": "EVIDENCE_HASH_MISMATCH"}


def _rejections(ev: Evidence, f: Findings) -> None:
    for r in ev.rejected:
        hard = [c for c in r.rejection_codes if c in HARD_REJECTIONS]
        tests = set(r.metadata.get("test_ids") or [])
        if not hard or not tests:
            continue
        later = [a for a in ev.intakes if a.adapter == r.adapter and a.submitted_at > r.submitted_at
                 and tests <= set(a.metadata.get("test_ids") or [])]
        if later:
            continue
        for c in hard:
            f.add(HARD_REJECTIONS[c], f"{r.adapter} evidence {r.evidence_id} for {sorted(tests)} was rejected "
                                      f"({c}): {'; '.join(r.problems)[:400]}", sorted(tests)[0],
                  evidence_ids=[r.evidence_id])


def _base(parameter: str) -> str:
    parts = parameter.split("_")
    return "_".join(parts[:-1]) if len(parts) > 1 and parameter_dimension(parameter) else parameter


def _limit_vs_requirement(tc: TestCaseSpec, intake: N4DesignIntake, f: Findings) -> None:
    reqs = {r.req_id: r for r in intake.requirements}
    for lim in tc.acceptance:
        for rid in tc.requirement_ids:
            r = reqs.get(rid)
            if r is None or r.tolerance is None or not isinstance(r.value, (int, float)) or r.unit is None:
                continue
            if _base(r.parameter) != lim.characteristic:
                continue
            v = Decimal(str(r.value))
            plus, minus = Decimal(str(r.tolerance.plus)), Decimal(str(r.tolerance.minus))
            lo, hi = (v - v * minus / 100, v + v * plus / 100) if r.tolerance.kind == "PCT" else (v - minus, v + plus)
            try:
                lo, hi = convert(lo, r.unit, lim.unit), convert(hi, r.unit, lim.unit)
            except (MetrologyError, ValueError):
                continue
            if (lim.lower is not None and lim.lower < lo) or (lim.upper is not None and lim.upper > hi) or \
                    lim.lower is None or lim.upper is None:
                f.add("LIMIT_LOOSER_THAN_REQUIREMENT", f"{tc.test_id} {lim.characteristic} limits [{lim.lower}, "
                                                       f"{lim.upper}] {lim.unit} are wider than {rid} "
                                                       f"[{lo.normalize()}, {hi.normalize()}]", tc.test_id, rid)


def _sim_measurements(tc: TestCaseSpec, intake: N4DesignIntake, plan: N4TestPlan) -> list[N4Measurement]:
    out = []
    for corner in intake.simulation_corners:
        if tc.sim_corner not in (None, "all", corner.get("name")):
            continue
        for lim in tc.acceptance:
            v = (corner.get("measures") or {}).get(lim.characteristic)
            if v is None or not corner.get("log_sha256"):
                continue
            out.append(N4Measurement(
                measurement_id=f"SIM-{tc.test_id}-{corner['name']}-{lim.characteristic}", tenant_id=plan.tenant_id,
                plan_id=plan.plan_id, evidence_id=f"n3:{corner.get('design_ref', 'circuit_simulation')}",
                test_id=tc.test_id, characteristic=lim.characteristic, unit_serial=f"corner:{corner['name']}",
                value=Decimal(str(v)), unit=lim.unit, expanded_uncertainty=tc.sim_model_uncertainty,
                calibration_source="NOT_APPLICABLE", captured_at=intake.locked_at, provenance="SIMULATION",
                method="SIM", method_ref=tc.method_ref, raw_sha256=corner["log_sha256"]))
    return out


def assess(plan: N4TestPlan, intake: N4DesignIntake, ev: Evidence, reg: Registry, pfmea: Optional[N4Pfmea],
           capas: dict[str, N4Capa], now: datetime, min_tur: float, skew_s: int
           ) -> tuple[list[N4Finding], list[TestVerdict], N4TraceabilityMatrix]:
    f = Findings()
    matrix = traceability(plan, intake)
    critical = safety_critical_ids(intake)
    reqs = {r.req_id: r for r in intake.requirements}
    for rid in matrix.uncovered_safety_critical:
        row = next(r for r in matrix.rows if r.requirement_id == rid)
        code = "SAFETY_REQUIRES_PHYSICAL" if "NO_PHYSICAL_METHOD" in row.gaps else "UNMAPPED_SAFETY_CRITICAL"
        f.add(code, f"safety-critical {rid} ({reqs[rid].parameter}; hazards {row.hazard_ids or '—'}) has "
                    + ("only simulation coverage" if code == "SAFETY_REQUIRES_PHYSICAL" else "no test case"),
              requirement_id=rid)
    for h in intake.hazards:
        if all(rid in matrix.uncovered_safety_critical for rid in h.requirement_ids):
            f.add("UNMAPPED_SAFETY_CRITICAL", f"hazard {h.hazard_id} ({h.description}, severity {h.severity}) has "
                                              "no verified mitigation path", requirement_id=h.requirement_ids[0])
    for rid in matrix.uncovered_other:
        f.add("REQUIREMENT_NOT_COVERED", f"{rid} ({reqs[rid].parameter}) has no test case", requirement_id=rid)
    for rid in matrix.unknown_requirement_refs:
        f.add("UNKNOWN_REQUIREMENT", f"test cases reference {rid}, which is not a requirement of "
                                     f"{intake.prd_artifact}", requirement_id=rid)

    by_test: dict[str, list[N4Measurement]] = {}
    for m in ev.measurements:
        by_test.setdefault(m.test_id, []).append(m)
    intakes = {e.evidence_id: e for e in ev.intakes}
    verdicts: list[TestVerdict] = []
    for tc in plan.test_cases:
        safety = bool(critical & set(tc.requirement_ids))
        sfx = "_SAFETY" if safety else ""
        if tc.owner is None:
            f.add("TEST_CASE_INCOMPLETE" + sfx, f"{tc.test_id} has no accountable owner", tc.test_id)
        method = None
        if tc.method_ref is None:
            f.add("METHOD_SOP_MISSING", f"{tc.test_id} does not reference a test SOP / method revision", tc.test_id)
        else:
            method = reg.methods.get((tc.method_ref.method_id, tc.method_ref.revision))
            if method is None or method.status != "APPROVED":
                f.add("METHOD_SOP_MISSING", f"{tc.test_id}: SOP {tc.method_ref.method_id} rev "
                                            f"{tc.method_ref.revision} is "
                                            f"{method.status if method else 'not registered'}", tc.test_id)
            elif method.method != tc.method:
                f.add("METHOD_SOP_MISSING", f"{tc.test_id}: SOP {method.method_id} is a {method.method} method, "
                                            f"the test is {tc.method}", tc.test_id)
        _limit_vs_requirement(tc, intake, f)
        ms = _sim_measurements(tc, intake, plan) if tc.method == "SIM" else by_test.get(tc.test_id, [])
        verified: list[VerifiedMeasurement] = []
        test_ok, any_result = True, False
        if tc.method == "SIM" and tc.sim_model_uncertainty is None:
            f.add("UNCERTAINTY_MISSING", f"{tc.test_id}: a SIM test needs a stated model uncertainty", tc.test_id)
            test_ok = False
        for lim in tc.acceptance:
            if lim.unit == "count" and tc.method == "INSPECTION":
                continue                                           # attribute inspection, judged below
            got = [m for m in ms if m.characteristic == lim.characteristic]
            if not got:
                continue
            any_result = True
            serials = {m.unit_serial for m in got}
            if tc.method != "SIM" and len(serials) < tc.sample_size:
                f.add("SAMPLE_SIZE_SHORT" + sfx, f"{tc.test_id} {lim.characteristic}: {len(serials)} of "
                                                 f"{tc.sample_size} units measured", tc.test_id)
                test_ok = False
            for m in got:
                vm, ok = _judge_measurement(m, tc, lim, intakes, ev, reg, f, now, min_tur, skew_s, safety)
                verified.append(vm)
                test_ok = test_ok and ok
        inspections = _attribute(tc, intakes, ev, plan, f) if tc.method == "INSPECTION" else []
        if inspections:
            any_result = True
            test_ok = test_ok and all(i.human_verdict == "ACCEPT" and not i.defects_confirmed and not i.unadjudicated
                                      for i in inspections)
        missing = [lim.characteristic for lim in tc.acceptance
                   if not (lim.unit == "count" and tc.method == "INSPECTION")
                   and not any(m.characteristic == lim.characteristic for m in ms)]
        if tc.method == "INSPECTION" and any(lim.unit == "count" for lim in tc.acceptance) and not inspections:
            missing.append("human inspection verdict")
        if missing:
            f.add("NOT_TESTED" + sfx, f"{tc.test_id} has no accepted evidence for {missing}", tc.test_id)
            test_ok = False
        verdict = "NOT_TESTED" if not any_result else (
            "FAIL" if any(v.verdict == "FAIL" for v in verified) else
            "INDETERMINATE" if any(v.verdict == "INDETERMINATE" for v in verified) or not test_ok else "PASS")
        verdicts.append(TestVerdict(test_id=tc.test_id, method=tc.method, verdict=verdict, measurements=verified,
                                    attribute_inspections=inspections))

    _rejections(ev, f)
    _pfmea_checks(plan, intake, pfmea, capas, {v.test_id: v.verdict for v in verdicts}, f)
    return f.items, verdicts, matrix


def _judge_measurement(m: N4Measurement, tc: TestCaseSpec, lim: AcceptanceLimit, intakes, ev: Evidence,
                       reg: Registry, f: Findings, now: datetime, min_tur: float, skew_s: int, safety: bool
                       ) -> tuple[VerifiedMeasurement, bool]:
    ok, tid, eids = True, tc.test_id, [m.evidence_id]
    j = judge(m.value, m.unit, m.expanded_uncertainty, lim)
    for code, msg in j.problems:
        f.add(code, f"{m.measurement_id}: {msg}", tid, evidence_ids=eids)
        ok = False
    if j.verdict == "FAIL":
        f.add("MEASUREMENT_FAIL", f"{tid} {m.unit_serial} {lim.characteristic} = {j.value} ± {j.uncertainty} "
                                  f"{lim.unit} is outside [{lim.lower}, {lim.upper}]", tid, evidence_ids=eids)
    elif j.verdict == "INDETERMINATE" and not j.problems:
        f.add("GUARD_BAND_INDETERMINATE", f"{tid} {m.unit_serial} {lim.characteristic} = {j.value} ± "
                                          f"{j.uncertainty} {lim.unit} crosses [{lim.lower}, {lim.upper}]: cannot "
                                          "be accepted", tid, evidence_ids=eids)
    ok = ok and j.verdict == "PASS"
    if j.tur is not None and j.tur < Decimal(str(min_tur)):
        f.add("LOW_TUR", f"{m.measurement_id}: test uncertainty ratio {j.tur.quantize(Decimal('0.01'))} < "
                         f"{min_tur}", tid, evidence_ids=eids)
    if m.provenance in ("SYNTHETIC", "MOCK_FIXTURE"):
        f.add("SYNTHETIC_NOT_QUALIFICATION", f"{m.measurement_id} is {m.provenance}; it can never qualify "
                                             f"{tc.method}", tid, evidence_ids=eids)
        ok = False
    elif m.provenance not in METHOD_PROVENANCE[tc.method]:
        f.add("PROVENANCE_METHOD_MISMATCH", f"{m.measurement_id}: {m.provenance} evidence cannot verify a "
                                            f"{tc.method} test", tid, evidence_ids=eids)
        ok = False
    cal_due = m.calibration_due
    if m.provenance in PHYSICAL_PROVENANCE:
        if m.calibration_source == "ACCREDITED_LAB":
            if m.calibration_due is None or not m.captured_at < m.calibration_due:
                f.add("CALIBRATION_EXPIRED", f"{m.measurement_id}: lab calibration due {m.calibration_due} not after "
                                             f"capture {m.captured_at}", tid, evidence_ids=eids)
                ok = False
        else:
            inst = reg.instruments.get(m.instrument_id or "")
            cal = reg.calibrations.get(m.calibration_id or "")
            cal_due = cal.calibration_due if cal else None
            for code, msg in calibration_problems(inst, cal, m.captured_at, now, skew_s):
                f.add(code, f"{m.measurement_id} ({m.instrument_id}): {msg}", tid, evidence_ids=eids)
                ok = False
    if m.method != "SIM" or tc.method == "SIM":
        if m.method_ref is None:
            f.add("METHOD_SOP_MISSING", f"{m.measurement_id} does not record the SOP revision used", tid,
                  evidence_ids=eids)
            ok = False
        elif tc.method_ref is not None and m.method_ref != tc.method_ref:
            f.add("METHOD_REVISION_MISMATCH", f"{m.measurement_id} used {m.method_ref.method_id} rev "
                                              f"{m.method_ref.revision}; the plan requires rev "
                                              f"{tc.method_ref.revision}", tid, evidence_ids=eids)
            ok = False
    if m.evidence_id in ev.tampered:
        f.add("EVIDENCE_HASH_MISMATCH", f"{m.evidence_id}: {ev.tampered[m.evidence_id]}", tid, evidence_ids=eids)
        ok = False
    vm = VerifiedMeasurement(
        measurement_id=m.measurement_id, test_id=tid, characteristic=lim.characteristic, unit_serial=m.unit_serial,
        value=j.value, unit=lim.unit, expanded_uncertainty=j.uncertainty, coverage_factor=m.coverage_factor,
        lower_limit=lim.lower, upper_limit=lim.upper, verdict=j.verdict, method=tc.method, provenance=m.provenance,
        instrument_id=m.instrument_id, calibration_id=m.calibration_id, calibration_due=cal_due,
        captured_at=m.captured_at, raw_sha256=m.raw_sha256)
    return vm, ok


def _attribute(tc: TestCaseSpec, intakes: dict[str, N4EvidenceIntake], ev: Evidence, plan: N4TestPlan,
               f: Findings) -> list[AttributeInspection]:
    """AOI: the model triages, a separate human decides. No human verdict, no result."""
    out = []
    for e in intakes.values():
        if e.adapter != "AOI_VISION" or e.metadata.get("test_id") != tc.test_id:
            continue
        adj = [a for a in ev.adjudications if a.evidence_id == e.evidence_id and a.independent]
        if e.evidence_id in ev.tampered:
            f.add("EVIDENCE_HASH_MISMATCH", f"{e.evidence_id}: {ev.tampered[e.evidence_id]}", tc.test_id,
                  evidence_ids=[e.evidence_id])
        if e.derived_provenance in ("SYNTHETIC", "MOCK_FIXTURE"):
            f.add("SYNTHETIC_NOT_QUALIFICATION", f"AOI evidence {e.evidence_id} is {e.derived_provenance}",
                  tc.test_id, evidence_ids=[e.evidence_id])
        flagged = [d for d in e.detections if d.triage in ("MODEL_FLAGGED", "AMBIGUOUS")]
        if not adj:
            f.add("AOI_UNADJUDICATED", f"{e.evidence_id}: {len(flagged)} model call(s) and the unit verdict await an "
                                       "independent human adjudication; model output never passes a unit",
                  tc.test_id, evidence_ids=[e.evidence_id])
            continue
        latest = adj[-1]
        labels = {d.detection_id: d.label for a in adj for d in a.decisions}
        pending = [d for d in flagged if d.detection_id not in labels]
        confirmed = [d for d, lab in labels.items() if lab == "CONFIRMED_DEFECT"]
        if pending:
            f.add("AOI_UNADJUDICATED", f"{e.evidence_id}: {[d.cls for d in pending]} not adjudicated", tc.test_id,
                  evidence_ids=[e.evidence_id])
        if confirmed or latest.unit_verdict == "REJECT":
            f.add("INSPECTION_DEFECT", f"{e.evidence_id}: {len(confirmed)} confirmed defect(s), unit verdict "
                                       f"{latest.unit_verdict}", tc.test_id, evidence_ids=[e.evidence_id])
        out.append(AttributeInspection(test_id=tc.test_id, unit_serial=e.metadata.get("unit_serial", "?"),
                                       provenance=e.derived_provenance or "PHYSICAL_INSPECTION",
                                       defects_confirmed=len(confirmed), unadjudicated=len(pending),
                                       human_verdict=latest.unit_verdict, adjudicator=latest.adjudicator,
                                       evidence_sha256=e.evidence_sha256))
    return out


def _pfmea_checks(plan: N4TestPlan, intake: N4DesignIntake, pfmea: Optional[N4Pfmea], capas: dict[str, N4Capa],
                  verdicts: dict[str, str], f: Findings) -> None:
    if pfmea is None:
        f.add("PFMEA_MISSING", f"plan {plan.plan_id} has no PFMEA")
        return
    tests = {tc.test_id for tc in plan.test_cases}
    hazards = {h.hazard_id for h in intake.hazards}
    covered_hazards = set()
    for e in pfmea.entries:
        covered_hazards |= set(e.hazard_ids)
        if e.severity >= SAFETY_SEVERITY:
            linked = [d.test_id for d in e.detection_controls if d.test_id in tests]
            if not linked:
                f.add("PFMEA_SAFETY_DETECTION_UNMAPPED", f"PFMEA {e.entry_id} (S={e.severity}, {e.failure_mode}) has "
                                                         "no detection control linked to a test case of this plan")
            open_ = [a.action_id for a in e.actions if a.status != "VERIFIED"]
            if open_:
                f.add("PFMEA_SAFETY_ACTION_OPEN", f"PFMEA {e.entry_id} (S={e.severity}) has open actions {open_}")
        for a in e.actions:
            if a.status == "VERIFIED" and (a.capa_id is None or capas.get(a.capa_id) is None
                                           or capas[a.capa_id].status != "VERIFIED_EFFECTIVE"):
                f.add("ACTION_VERIFICATION_UNSUPPORTED", f"PFMEA {e.entry_id} action {a.action_id} is VERIFIED without "
                                                         "a CAPA verified effective by quality review")
        if e.attention_level == "HIGH" and not e.actions:
            f.add("PFMEA_HIGH_ATTENTION", f"PFMEA {e.entry_id}: {e.attention_basis}")
    for h in sorted(hazards - covered_hazards):
        f.add("PFMEA_HAZARD_UNADDRESSED", f"hazard {h} has no PFMEA entry")


__all__ = ["ENGINE_VERSION", "RULES", "ApTable", "Evidence", "Registry", "assess", "attention", "decide",
           "rate_entry", "safety_critical_ids", "traceability", "unit_info", "AoiDetection"]
