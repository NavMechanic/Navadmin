"""Deterministic N3 engine: planning (N3.2), verification (N3.5), decision + rework proposals (N3.6) and
release payload assembly (N3.7). No tool execution and no I/O beyond the ``read(sha256)`` callback.

Decision precedence is BLOCK > REWORK > PASS. Hard safety and engineering rules are evaluated
before any cost logic, and the cost-aware rework proposals never change the decision.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional

from ..models import REQUIRED_CORNERS, DesignConstraints, TestLimit
from ..repository import new_id
from . import APPROVAL_SCOPE, parsers
from .models import (KIND_REQUIREMENTS, KIND_SCOPE, ActionCandidate, Corner, N3DesignBaseline, N3FileEvidence,
                     N3Finding, N3WorkPackage, RankedProposal)

ENGINE_VERSION = "n3-engine/1.0.0"
CLEARANCE_TYPES = {"clearance", "creepage", "hole_clearance", "copper_edge_clearance", "edge_clearance",
                   "silk_edge_clearance"}

#: code → (severity, outcome, category, description)
RULES: dict[str, tuple[str, str, str, str]] = {
    "NATIVE_FILES_MISSING": ("CRITICAL", "BLOCK", "DESIGN_INTEGRITY", "required native editable source files missing"),
    "NATIVE_GEOMETRY_MISSING": ("CRITICAL", "BLOCK", "DESIGN_INTEGRITY", "no native STEP / CAD geometry"),
    "DESIGN_FILE_UNVERIFIED": ("CRITICAL", "BLOCK", "DESIGN_INTEGRITY", "native file without a tool version"),
    "INVALID_NATIVE_FILE": ("CRITICAL", "BLOCK", "DESIGN_INTEGRITY", "file content does not match its native format"),
    "REVISION_MISMATCH": ("CRITICAL", "BLOCK", "REVISION_CONTROL", "design files disagree on the design revision"),
    "VERIFICATION_EVIDENCE_MISSING": ("MAJOR", "REWORK", "EVIDENCE", "required verification evidence missing"),
    "REPORT_UNPARSEABLE": ("MAJOR", "REWORK", "EVIDENCE", "a tool report could not be parsed"),
    "CRITICAL_ERC": ("CRITICAL", "BLOCK", "SAFETY", "unresolved ERC error"),
    "CRITICAL_DRC": ("CRITICAL", "BLOCK", "SAFETY", "unresolved DRC error"),
    "CREEPAGE_CLEARANCE": ("CRITICAL", "BLOCK", "SAFETY", "creepage / clearance violation"),
    "UNROUTED_NETS": ("CRITICAL", "BLOCK", "SAFETY", "unconnected items on the board"),
    "SCHEMATIC_PCB_PARITY": ("CRITICAL", "BLOCK", "REVISION_CONTROL", "PCB does not match the schematic"),
    "ERC_DRC_WARNING": ("MINOR", "INFO", "QUALITY", "ERC / DRC warning"),
    "BOM_REVISION_MISMATCH": ("CRITICAL", "BLOCK", "REVISION_CONTROL", "BOM / schematic and PCB revisions differ"),
    "BOM_PCB_REFDES_MISMATCH": ("CRITICAL", "BLOCK", "REVISION_CONTROL", "BOM refdes do not match the PCB"),
    "BOM_LINE_INVALID": ("MAJOR", "REWORK", "BOM", "BOM line incomplete or inconsistent"),
    "CLEARANCE_RULE_UNVERIFIED": ("MAJOR", "REWORK", "SAFETY", "board clearance rule could not be checked"),
    "LAYER_COUNT_EXCEEDED": ("MAJOR", "REWORK", "DFM", "copper layer count above the PRD limit"),
    "MANUFACTURING_OUTPUTS_MISSING": ("MAJOR", "REWORK", "DFM", "Gerber + drill or ODB++ outputs missing"),
    "CORNER_DEFINITION_INCOMPLETE": ("CRITICAL", "BLOCK", "SAFETY", "PRD lacks values to define worst-case corners"),
    "WORST_CASE_CORNER_MISSING": ("CRITICAL", "BLOCK", "SAFETY", "a required worst-case corner was not simulated"),
    "SIMULATION_NOT_CONVERGED": ("MAJOR", "REWORK", "SIMULATION", "a corner did not converge"),
    "SOLVER_VERSION_MISSING": ("MAJOR", "REWORK", "SIMULATION", "solver version not captured"),
    "CORNER_MISCONFIGURED": ("MAJOR", "REWORK", "SIMULATION", "corner ran at the wrong temperature"),
    "SIM_SPEC_VIOLATION": ("MAJOR", "REWORK", "SIMULATION", "a measured value is outside its PRD test limit"),
    "DERATING_BREACH": ("CRITICAL", "BLOCK", "SAFETY", "component stress above its derated rating"),
    "THERMAL_BOUNDARY_MISSING": ("MAJOR", "REWORK", "THERMAL", "thermal boundary conditions missing"),
    "THERMAL_ASSUMPTIONS_MISSING": ("MAJOR", "REWORK", "THERMAL", "ambient / load / limits missing"),
    "THERMAL_NOT_WORST_CASE": ("CRITICAL", "BLOCK", "SAFETY", "thermal analysis below the PRD worst case"),
    "THERMAL_LIMIT_EXCEEDED": ("CRITICAL", "BLOCK", "SAFETY", "component temperature above limit minus margin"),
}


class Findings:
    def __init__(self) -> None:
        self.items: list[N3Finding] = []

    def add(self, code: str, message: str, evidence: Iterable[str] = (), refdes: Optional[str] = None,
            corner: Optional[str] = None) -> None:
        sev, outcome, cat, _ = RULES[code]
        self.items.append(N3Finding(finding_id=new_id("N3F"), code=code, severity=sev, outcome=outcome, category=cat,
                                    message=message, evidence=list(evidence), refdes=refdes, corner=corner))


def decide(findings: Iterable[N3Finding]) -> str:
    outcomes = {f.outcome for f in findings}
    return "BLOCK" if "BLOCK" in outcomes else "REWORK" if "REWORK" in outcomes else "PASS"


# ----------------------------------------------------------------------------------------------
# N3.2 planning
# ----------------------------------------------------------------------------------------------
def acceptance_criteria(kind: str, c: DesignConstraints) -> list[str]:
    req = KIND_REQUIREMENTS[kind]
    out = [f"native editable files: {', '.join(req['native'])} with SHA-256 and tool version",
           f"verification evidence: {', '.join(req['evidence']) or 'none beyond native geometry'}",
           "zero unresolved critical findings; one consistent design revision"]
    if kind == "PCB_LAYOUT":
        if c.min_clearance_mm:
            out.append(f"board clearance rule ≥ {c.min_clearance_mm} mm")
        out.append("BOM refdes reconcile 1:1 with PCB footprints; BOM revision = PCB revision")
    if kind == "CIRCUIT_SIMULATION":
        out.append(f"converged worst-case corners {', '.join(REQUIRED_CORNERS)}; stresses ≤ {c.derating_factor} × rating")
    if kind == "THERMAL":
        out.append(f"ambient ≥ {c.ambient_max_c} °C, temperatures ≤ limit − {c.thermal_margin_c} °C")
    if kind == "DFM" and c.max_pcb_layers:
        out.append(f"≤ {c.max_pcb_layers} copper layers; Gerber + drill or ODB++ outputs")
    return out


def default_corners(c: DesignConstraints) -> tuple[list[Corner], list[str]]:
    missing = [n for n in ("input_voltage_min_v", "input_voltage_max_v", "load_max_a", "ambient_max_c")
               if getattr(c, n) is None]
    if missing:
        return [], missing
    vnom = (c.input_voltage_min_v + c.input_voltage_max_v) / 2
    return [Corner(name="vin_min", params={"vin": c.input_voltage_min_v, "iload": c.load_max_a}, temp_c=25.0),
            Corner(name="vin_max", params={"vin": c.input_voltage_max_v, "iload": c.load_max_a}, temp_c=25.0),
            Corner(name="temp_max", params={"vin": vnom, "iload": c.load_max_a}, temp_c=c.ambient_max_c),
            Corner(name="load_max", params={"vin": c.input_voltage_min_v, "iload": c.load_max_a},
                   temp_c=c.ambient_max_c)], []


# ----------------------------------------------------------------------------------------------
# N3.5 verification
# ----------------------------------------------------------------------------------------------
@dataclass
class Assessed:
    findings: list[N3Finding]
    metrics: dict[str, Any]
    design_revision: Optional[str]


def _latest_by_role(files: list[N3FileEvidence]) -> dict[str, list[N3FileEvidence]]:
    out: dict[str, list[N3FileEvidence]] = {}
    for f in files:
        out.setdefault(f.role, []).append(f)
    return out


def assess(wp: N3WorkPackage, baseline: N3DesignBaseline, files: list[N3FileEvidence],
           read: Callable[[str], bytes], manifest_revision: Optional[str], job_meta: dict[str, dict]) -> Assessed:
    f, metrics = Findings(), {}
    by = _latest_by_role(files)
    req = KIND_REQUIREMENTS[wp.kind]
    for role in req["native"]:
        if role not in by:
            only_docs = files and all(x.role in ("DOCUMENT", "IMAGE") for x in files)
            code = "NATIVE_GEOMETRY_MISSING" if role == "GEOMETRY" else "NATIVE_FILES_MISSING"
            f.add(code, f"{wp.kind} needs a native {role} file" + (" — the delivery is PDF/screenshot-only"
                                                                    if only_docs else ""))
    for x in files:
        if x.native and not x.tool_version:
            f.add("DESIGN_FILE_UNVERIFIED", f"{x.path}: tool version unknown", [x.sha256])
        if x.detected_format == "invalid":
            f.add("INVALID_NATIVE_FILE", f"{x.path} does not contain what its extension claims", [x.sha256])
    for role in req["evidence"]:
        if role not in by and not (wp.kind == "DFM" and "ODB" in by and role in ("GERBER", "DRILL")):
            code = "MANUFACTURING_OUTPUTS_MISSING" if role in ("GERBER", "DRILL") else "VERIFICATION_EVIDENCE_MISSING"
            f.add(code, f"{wp.kind} needs {role} evidence (deliver it or run the tool job)")
    revs = {x.revision for x in files if x.native and x.revision}
    design_revision = manifest_revision or (next(iter(revs)) if len(revs) == 1 else None)
    if len(revs) > 1:
        f.add("REVISION_MISMATCH", f"native files carry revisions {sorted(revs)}", [x.sha256 for x in files if x.revision])
    elif revs and manifest_revision and revs != {manifest_revision}:
        f.add("REVISION_MISMATCH", f"files are revision {revs.pop()} but the delivery declares {manifest_revision}")
    metrics["files"] = len(files)
    checker = {"ELECTRICAL_SCHEMATIC": _schematic, "PCB_LAYOUT": _pcb, "DFM": _dfm, "CIRCUIT_SIMULATION": _simulation,
               "MECHANICAL": _mechanical, "THERMAL": _thermal}[wp.kind]
    checker(f, metrics, by, read, baseline, job_meta)
    metrics["design_revision"] = design_revision
    return Assessed(f.items, metrics, design_revision)


def _report(f: Findings, metrics: dict, by: dict, read, role: str, kind: str) -> Optional[parsers.KicadReport]:
    rep_file = (by.get(role) or [None])[-1]
    if rep_file is None:
        return None
    try:
        rep = parsers.parse_kicad_report(read(rep_file.sha256))
    except (ValueError, KeyError, TypeError) as exc:
        f.add("REPORT_UNPARSEABLE", f"{rep_file.path}: {exc}", [rep_file.sha256])
        return None
    crit = errors = warnings = waived = 0
    for v in rep.violations:
        if v.excluded:
            waived += 1
            continue
        if v.severity == "error":
            crit += 1
            errors += 1
            code = "CREEPAGE_CLEARANCE" if kind == "drc" and v.type in CLEARANCE_TYPES else \
                ("CRITICAL_ERC" if kind == "erc" else "CRITICAL_DRC")
            f.add(code, f"{kind.upper()} {v.type}: {v.description}", [rep_file.sha256])
        elif v.severity == "warning":
            warnings += 1
            f.add("ERC_DRC_WARNING", f"{kind.upper()} warning {v.type}: {v.description}", [rep_file.sha256])
    if rep.unconnected:
        crit += rep.unconnected
        f.add("UNROUTED_NETS", f"{rep.unconnected} unconnected item(s)", [rep_file.sha256])
    if rep.parity:
        crit += rep.parity
        f.add("SCHEMATIC_PCB_PARITY", f"{rep.parity} schematic/PCB parity issue(s)", [rep_file.sha256])
    metrics[kind] = {"tool": "kicad-cli", "tool_version": rep.tool_version, "critical": crit, "errors": errors,
                     "warnings": warnings, "waived": waived, "report_sha256": rep_file.sha256}
    return rep


def _bom(f: Findings, metrics: dict, by: dict, read) -> Optional[parsers.ParsedBom]:
    bom_file = (by.get("BOM") or [None])[-1]
    if bom_file is None:
        return None
    bom = parsers.parse_bom_csv(read(bom_file.sha256))
    for p in bom.problems:
        f.add("BOM_LINE_INVALID", p, [bom_file.sha256])
    seen = {}
    for it in bom.items:
        if not it.dnp and not it.mpn:
            f.add("BOM_LINE_INVALID", f"BOM line {it.line} ({', '.join(it.refdes)}) has no MPN", [bom_file.sha256])
        if not it.dnp and it.qty != len(it.refdes):
            f.add("BOM_LINE_INVALID", f"BOM line {it.line}: qty {it.qty} ≠ {len(it.refdes)} refdes", [bom_file.sha256])
        for r in it.refdes:
            if r in seen:
                f.add("BOM_LINE_INVALID", f"{r} appears on BOM lines {seen[r]} and {it.line}", [bom_file.sha256])
            seen.setdefault(r, it.line)
    metrics["bom"] = {"items": [it.model_dump(mode="json") for it in bom.items], "sha256": bom_file.sha256,
                      "lines": len(bom.items)}
    return bom


def _schematic(f, metrics, by, read, baseline, job_meta) -> None:
    _report(f, metrics, by, read, "ERC_REPORT", "erc")
    _bom(f, metrics, by, read)


def _pcb(f, metrics, by, read, baseline, job_meta) -> None:
    _report(f, metrics, by, read, "DRC_REPORT", "drc")
    bom = _bom(f, metrics, by, read)
    pcb = (by.get("PCB") or [None])[-1]
    sch = (by.get("SCHEMATIC") or [None])[-1]
    c = baseline.constraints
    if pcb is not None:
        text = parsers._text(read(pcb.sha256))
        refs = parsers.pcb_refdes(text)
        layers = parsers.pcb_copper_layers(text)
        metrics["pcb"] = {"revision": pcb.revision, "refdes": len(refs), "copper_layers": layers}
        if bom is not None:
            bom_refs = {r for it in bom.items if not it.dnp for r in it.refdes}
            missing, extra = sorted(bom_refs - refs), sorted(refs - bom_refs)
            if missing or extra:
                f.add("BOM_PCB_REFDES_MISMATCH", f"BOM refdes not on the PCB: {missing or '—'}; PCB footprints not in "
                                                 f"the BOM: {extra or '—'}", [pcb.sha256])
        if sch is not None and sch.revision and pcb.revision and sch.revision != pcb.revision:
            f.add("BOM_REVISION_MISMATCH", f"schematic/BOM revision {sch.revision} ≠ PCB revision {pcb.revision}",
                  [sch.sha256, pcb.sha256])
        if c.max_pcb_layers and layers > c.max_pcb_layers:
            f.add("LAYER_COUNT_EXCEEDED", f"{layers} copper layers > PRD limit {c.max_pcb_layers}", [pcb.sha256])
    if c.min_clearance_mm:
        pro = (by.get("PROJECT") or [None])[-1]
        actual = None
        if pro is not None:
            try:
                actual = json.loads(read(pro.sha256))["board"]["design_settings"]["rules"]["min_clearance"]
            except (ValueError, KeyError, TypeError):
                actual = None
        if actual is None:
            f.add("CLEARANCE_RULE_UNVERIFIED", f"PRD requires ≥ {c.min_clearance_mm} mm clearance; no readable "
                                               ".kicad_pro rule set was delivered")
        else:
            metrics["clearance"] = [{"kind": "clearance", "required_mm": c.min_clearance_mm, "actual_mm": actual}]
            if float(actual) < c.min_clearance_mm:
                f.add("CREEPAGE_CLEARANCE", f"board min clearance {actual} mm < PRD {c.min_clearance_mm} mm",
                      [pro.sha256])


def _dfm(f, metrics, by, read, baseline, job_meta) -> None:
    _report(f, metrics, by, read, "DRC_REPORT", "drc")
    pcb = (by.get("PCB") or [None])[-1]
    if pcb is not None and baseline.constraints.max_pcb_layers:
        layers = parsers.pcb_copper_layers(parsers._text(read(pcb.sha256)))
        if layers > baseline.constraints.max_pcb_layers:
            f.add("LAYER_COUNT_EXCEEDED", f"{layers} copper layers > PRD limit {baseline.constraints.max_pcb_layers}",
                  [pcb.sha256])
    metrics["manufacturing_outputs"] = sorted({x.path for r in ("GERBER", "DRILL", "ODB") for x in by.get(r, [])})


def _simulation(f, metrics, by, read, baseline, job_meta) -> None:
    c = baseline.constraints
    corners, missing_defs = default_corners(c)
    if missing_defs:
        f.add("CORNER_DEFINITION_INCOMPLETE", f"PRD constraints lack {', '.join(missing_defs)}")
    logs = {}
    for x in by.get("SIM_LOG", []):
        name = x.path.rsplit("/", 1)[-1].removesuffix(".log").removeprefix("corner_")
        logs[name] = x
    bom = _bom(f, metrics, by, read) if "BOM" in by else None
    ratings = bom.ratings if bom else {}
    expected = {k.name: k for k in corners}
    results, solver_versions = [], set()
    for name in REQUIRED_CORNERS:
        log = logs.get(name)
        if log is None:
            f.add("WORST_CASE_CORNER_MISSING", f"no simulation log for worst-case corner {name}", corner=name)
            continue
        meta = job_meta.get(log.sha256, {})
        sl = parsers.parse_ngspice_log(read(log.sha256), meta.get("exit_code"))
        version = sl.version or log.tool_version
        if version:
            solver_versions.add(version)
        if not sl.converged:
            f.add("SIMULATION_NOT_CONVERGED", f"corner {name}: {'; '.join(sl.errors) or 'no analysis output'}",
                  [log.sha256], corner=name)
        exp = expected.get(name)
        if exp is not None and sl.temp_c is not None and abs(sl.temp_c - exp.temp_c) > 0.01:
            f.add("CORNER_MISCONFIGURED", f"corner {name} ran at {sl.temp_c} °C, expected {exp.temp_c} °C",
                  [log.sha256], corner=name)
        for lim in baseline.test_limits:
            v = sl.measures.get(lim.parameter.lower())
            if v is not None and ((lim.min is not None and v < lim.min) or (lim.max is not None and v > lim.max)):
                f.add("SIM_SPEC_VIOLATION", f"corner {name}: {lim.parameter} = {v:g} outside "
                                            f"[{lim.min}, {lim.max}] {lim.unit or ''}".rstrip(), [log.sha256], corner=name)
        for mname, value in sl.measures.items():
            if mname.startswith("stress_"):
                parts = mname.split("_")
                if len(parts) >= 3:
                    ref, kind = parts[1], parts[2][0]
                    rating = ratings.get(ref, {}).get(kind)
                    if rating:
                        metrics.setdefault("derating", []).append(
                            {"refdes": ref.upper(), "parameter": parts[2], "stress": abs(value), "rating": rating,
                             "factor": c.derating_factor, "corner": name})
                        if abs(value) > c.derating_factor * rating:
                            f.add("DERATING_BREACH", f"corner {name}: {ref.upper()} {parts[2]} {abs(value):g} > "
                                                     f"{c.derating_factor} × {rating:g}", [log.sha256],
                                  refdes=ref.upper(), corner=name)
        results.append({"name": name, "converged": sl.converged, "measures": sl.measures, "log_sha256": log.sha256,
                        "parameters": exp.params if exp else {}, "temp_c": sl.temp_c})
    if logs and not solver_versions:
        f.add("SOLVER_VERSION_MISSING", "no ngspice version in the logs or the tool job")
    metrics["simulation"] = {"solver": "ngspice", "solver_version": sorted(solver_versions)[-1] if solver_versions
                             else None, "required_corners": list(REQUIRED_CORNERS),
                             "corners": [{k: v for k, v in r.items() if k != "temp_c"} for r in results]}


def _mechanical(f, metrics, by, read, baseline, job_meta) -> None:
    geo = by.get("GEOMETRY", [])
    metrics["geometry"] = [{"path": x.path, "sha256": x.sha256, "tool": x.tool, "tool_version": x.tool_version}
                           for x in geo]
    if "BOM" in by:
        _bom(f, metrics, by, read)


def _thermal(f, metrics, by, read, baseline, job_meta) -> None:
    rep = (by.get("THERMAL_REPORT") or [None])[-1]
    if rep is None:
        return
    try:
        t = parsers.parse_thermal_report(read(rep.sha256))
    except (ValueError, TypeError) as exc:
        f.add("REPORT_UNPARSEABLE", f"{rep.path}: {exc}", [rep.sha256])
        return
    c = baseline.constraints
    if not t.get("boundary_conditions"):
        f.add("THERMAL_BOUNDARY_MISSING", "thermal report has no boundary conditions", [rep.sha256])
    if not t.get("solver") or not t.get("solver_version"):
        f.add("SOLVER_VERSION_MISSING", "thermal report must name its solver and version", [rep.sha256])
    amb, load = t.get("ambient_c"), t.get("load_w")
    if amb is None or load is None:
        f.add("THERMAL_ASSUMPTIONS_MISSING", "ambient_c and load_w assumptions are required", [rep.sha256])
    if amb is not None and c.ambient_max_c is not None and float(amb) < c.ambient_max_c:
        f.add("THERMAL_NOT_WORST_CASE", f"analysed at {amb} °C ambient; PRD maximum is {c.ambient_max_c} °C",
              [rep.sha256])
    if load is not None and c.load_max_w is not None and float(load) < c.load_max_w:
        f.add("THERMAL_NOT_WORST_CASE", f"analysed at {load} W; PRD maximum load is {c.load_max_w} W", [rep.sha256])
    limits = t.get("limits_c") or {}
    for ref, temp in sorted((t.get("max_temps_c") or {}).items()):
        lim = limits.get(ref)
        if lim is None:
            f.add("THERMAL_ASSUMPTIONS_MISSING", f"no temperature limit for {ref}", [rep.sha256], refdes=ref)
        elif float(temp) > float(lim) - c.thermal_margin_c:
            f.add("THERMAL_LIMIT_EXCEEDED", f"{ref} {temp} °C > {lim} °C − {c.thermal_margin_c} °C margin",
                  [rep.sha256], refdes=ref)
    metrics["thermal"] = {"solver": t.get("solver"), "solver_version": t.get("solver_version"),
                          "boundary_conditions": t.get("boundary_conditions") or {}, "ambient_c": amb, "load_w": load,
                          "max_temps_c": t.get("max_temps_c") or {}, "limits_c": limits,
                          "margin_c": c.thermal_margin_c, "report_sha256": rep.sha256}


# ----------------------------------------------------------------------------------------------
# N3.6 rework proposals (never executed, never change the decision)
# ----------------------------------------------------------------------------------------------
def rank_actions(findings: list[N3Finding], candidates: list[ActionCandidate], budget_cap: Optional[Decimal]
                 ) -> tuple[list[RankedProposal], list[dict]]:
    open_codes = {f.code for f in findings if f.outcome in ("BLOCK", "REWORK")}
    block_codes = {f.code for f in findings if f.outcome == "BLOCK"}
    blocked, scored = [], []
    for a in candidates:
        if a.action_type == "WAIVER_REQUEST" and set(a.addresses) & block_codes:
            blocked.append({"action_id": a.action_id, "codes": ["BLOCK_NOT_WAIVABLE"],
                            "reason": "hard safety / engineering findings cannot be waived by a cost decision"})
            continue
        if not set(a.addresses) & open_codes:
            blocked.append({"action_id": a.action_id, "codes": ["NOT_RELEVANT"],
                            "reason": f"addresses {a.addresses}, none of which is an open finding"})
            continue
        total = a.cost + a.delay_cost_per_day * a.delay_days
        scored.append((a, total, a.risk_reduction_value - total))
    scored.sort(key=lambda x: (-x[2], x[1], x[0].action_id))
    out, spent = [], Decimal(0)
    for i, (a, total, net) in enumerate(scored, 1):
        within = budget_cap is None or spent + total <= budget_cap
        rec = net > 0 and within
        if rec:
            spent += total
        out.append(RankedProposal(rank=i, action_id=a.action_id, action_type=a.action_type,
                                  addresses=sorted(set(a.addresses) & open_codes), total_cost=total, net_value=net,
                                  recommended=rec,
                                  rationale=f"value {a.risk_reduction_value} − cost {total} = {net}"
                                            + ("" if within else "; over budget cap")))
    return out, blocked


# ----------------------------------------------------------------------------------------------
# N3.7 release payloads
# ----------------------------------------------------------------------------------------------
def design_release_payload(release_id: str, scope: str, baseline: N3DesignBaseline, assessments: list,
                           review_ids: list[str], design_revision: str) -> dict:
    files, payload = [], {"release_id": release_id, "scope": scope, "prd_ref": baseline.prd_artifact,
                          "prd_payload_sha256": baseline.prd_payload_sha256, "design_revision": design_revision,
                          "assessment_ids": [a.assessment_id for a in assessments], "review_ids": review_ids,
                          "test_limits": [t.model_dump(mode="json") for t in baseline.test_limits],
                          "approval_scope": APPROVAL_SCOPE, "derating": [], "clearance": []}
    seen = set()
    for a in assessments:
        for x in a.files:
            if x.sha256 not in seen and x.role not in ("DOCUMENT", "IMAGE", "OTHER"):
                seen.add(x.sha256)
                files.append({"path": x.path, "role": x.role, "sha256": x.sha256, "tool": x.tool,
                              "tool_version": x.tool_version, "blob_uri": x.uri})
        m = a.metrics
        for key in ("erc", "drc"):
            if key in m:
                payload[key] = {k: m[key][k] for k in ("tool", "tool_version", "critical", "errors", "warnings",
                                                       "report_sha256", "waived")}
        if "simulation" in m:
            payload["simulation"] = m["simulation"]
        if "thermal" in m:
            payload["thermal"] = {k: m["thermal"][k] for k in ("solver", "solver_version", "boundary_conditions",
                                                               "ambient_c", "load_w", "max_temps_c", "limits_c",
                                                               "margin_c")}
        payload["derating"] += [{k: d[k] for k in ("refdes", "parameter", "stress", "rating", "factor")}
                                for d in m.get("derating", [])]
        payload["clearance"] += m.get("clearance", [])
    payload["files"] = files
    return payload


def bom_release_payload(release_id: str, scope: str, baseline: N3DesignBaseline, assessments: list,
                        design_revision: str) -> Optional[dict]:
    bom = next((a.metrics["bom"] for a in reversed(assessments) if "bom" in a.metrics), None)
    if bom is None:
        return None
    pcb_rev = next((a.metrics["pcb"]["revision"] for a in assessments if "pcb" in a.metrics), None)
    return {"release_id": release_id, "scope": scope, "prd_ref": baseline.prd_artifact,
            "design_revision": design_revision, "pcb_revision": pcb_rev, "bom_source_sha256": bom["sha256"],
            "items": bom["items"]}


__all__ = ["ENGINE_VERSION", "RULES", "assess", "decide", "default_corners", "acceptance_criteria", "rank_actions",
           "design_release_payload", "bom_release_payload", "KIND_SCOPE", "TestLimit"]
