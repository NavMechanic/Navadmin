"""The 27 deterministic Data Quality rules DQ01–DQ27.

Each rule is a pure function of a ``RuleContext`` returning ``(ok, message[, error_code])``;
``ok=None`` means "not applicable". Rules never mutate state. They may record measurements in
``ctx.metrics`` that the scorer turns into dimension scores.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel

from .handoffs import HandoffLedger
from .manifest import (AI_KINDS, APPROVED_RIGHTS, LANGUAGE_MODEL_KINDS, SHA256, CanonicalIOManifest, lineage_hash,
                       parse_ref, sha256_bytes)
from .registry import ArtifactRecord, ArtifactRegistry, EvidenceRecord
from .schemas import FRESHNESS_SECONDS, SCHEMAS, SchemaSpec
from .units import is_si_compatible

Severity = Literal["CRITICAL", "MAJOR"]
Action = Literal["BLOCK", "QUARANTINE", "REWORK"]
SIDE_EFFECTS = ("PO", "TENDER_BID", "DESIGN_RELEASE", "CERTIFICATION")
NATIVE_DESIGN_EXT = (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".step", ".stp")
INPUT_SCHEMAS = frozenset(SCHEMAS) | {"raw_file.v1", "work_package.v1", "plc_message.v1", "rights_grant.v1",
                                      "nextest_log.v1", "fea_mesh_file.v1", "step_file.v1", "pdf_file.v1"}
_PLACEHOLDER = re.compile(r"^(tbd|tba|unknown|n/?a|none|generic|any|-+)$", re.I)
_SHA = re.compile(SHA256)
_ID = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    severity: Severity
    passed: bool
    action_on_fail: Action
    error_code: str
    message: str
    applicable: bool = True


@dataclass
class ApprovalRecord:
    event_id: str
    reviewer_id: str
    gate: str
    approved_at: datetime
    notes: str = ""


@dataclass
class RuleContext:
    manifest: CanonicalIOManifest
    payload: dict
    model: Optional[BaseModel]
    schema_errors: list[str]
    missing_errors: list[str]
    spec: Optional[SchemaSpec]
    content: Optional[bytes]
    files: dict[str, bytes]
    required_critical: tuple[str, ...]
    tenant_id: str
    actor_id: str
    registry: ArtifactRegistry
    handoffs: HandoffLedger
    now: datetime
    side_effect: Optional[str] = None
    use_for_training: bool = False
    ai_provenance: Optional[dict] = None
    approval: Optional[ApprovalRecord] = None
    stream_state: dict = field(default_factory=dict)
    simulator_source: bool = False
    metrics: dict = field(default_factory=dict)

    # helpers ---------------------------------------------------------------------------------
    def resolved_sources(self) -> list[ArtifactRecord]:
        return [r for r in (self.registry.get(ref) for ref in self.manifest.source_artifact_ids) if r is not None]

    def linked(self, schema: str) -> list[ArtifactRecord]:
        return [r for r in self.resolved_sources() if r.schema_key == schema]

    def visible_evidence(self, evidence_id: str) -> Optional[EvidenceRecord]:
        ev = self.registry.evidence(evidence_id)
        return ev if ev is not None and ev.tenant_id == self.tenant_id else None


CheckOutcome = tuple  # (Optional[bool], str) or (Optional[bool], str, error_code)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    scope: str            # "ALL" or a node id
    severity: Severity
    action: Action
    error_code: str
    description: str
    check: Callable[[RuleContext], CheckOutcome]

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        if self.scope != "ALL" and ctx.manifest.node_id != self.scope:
            return RuleResult(self.rule_id, self.severity, True, self.action, "", f"not applicable to {ctx.manifest.node_id}",
                              applicable=False)
        out = self.check(ctx)
        ok, message = out[0], out[1]
        code = out[2] if len(out) > 2 else self.error_code
        if ok is None:
            return RuleResult(self.rule_id, self.severity, True, self.action, "", message, applicable=False)
        return RuleResult(self.rule_id, self.severity, bool(ok), self.action, "" if ok else code, message)


RULES: dict[str, Rule] = {}


def rule(rule_id: str, scope: str, severity: Severity, action: Action, error_code: str, description: str):
    def register(fn):
        RULES[rule_id] = Rule(rule_id, scope, severity, action, error_code, description, fn)
        return fn
    return register


def evaluate_rules(ctx: RuleContext, only: Optional[set[str]] = None) -> list[RuleResult]:
    return [r.evaluate(ctx) for rid, r in sorted(RULES.items()) if only is None or rid in only]


def _present(payload: dict, path: str) -> bool:
    cur: Any = payload
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return cur is not None and cur != "" and cur != [] and cur != {}


def _in_date(ev: EvidenceRecord, at: datetime) -> bool:
    return (ev.valid_from is None or ev.valid_from <= at) and (ev.valid_until is None or at <= ev.valid_until)


# --- global rules -------------------------------------------------------------------------------

@rule("DQ01", "ALL", "CRITICAL", "BLOCK", "RIGHTS_NOT_APPROVED",
      "source_license_rights APPROVED/APPROVED_NO_TRAIN and a G0 grant before vendor/model execution")
def dq01(ctx: RuleContext):
    m = ctx.manifest
    if m.source_license_rights not in APPROVED_RIGHTS:
        return False, f"source_license_rights is {m.source_license_rights}"
    grant = ctx.registry.rights_grant(m.project_id)
    if grant is None or grant.status != "ACTIVE" or grant.tenant_id != ctx.tenant_id:
        return False, f"no active G0 rights/NDA grant for project {m.project_id}"
    return True, f"rights {m.source_license_rights}; G0 grant {grant.ref}"


@rule("DQ02", "ALL", "CRITICAL", "QUARANTINE", "SCHEMA_INVALID",
      "input/output schemas registered and payload 100% schema-compliant")
def dq02(ctx: RuleContext):
    m = ctx.manifest
    if m.output_schema_key not in SCHEMAS:
        return False, f"output_schema {m.output_schema} is not a registered schema"
    from .manifest import schema_key
    if schema_key(m.input_schema) not in INPUT_SCHEMAS:
        return False, f"input_schema {m.input_schema} is not a registered schema"
    if ctx.schema_errors:
        return False, "payload violates schema: " + "; ".join(ctx.schema_errors[:5])
    return True, f"payload conforms to {m.output_schema_key}"


@rule("DQ03", "ALL", "CRITICAL", "REWORK", "REQ_CRITICAL_MISSING",
      "100% required_critical[] present; zero silent defaults")
def dq03(ctx: RuleContext):
    critical = list(ctx.spec.critical if ctx.spec else ()) + list(ctx.required_critical)
    missing = sorted({p for p in critical if not _present(ctx.payload, p)} | set(ctx.missing_errors))
    ctx.metrics["critical"] = (len(critical) - len([p for p in critical if p in missing]), len(critical))
    if missing:
        return False, f"critical fields missing or empty (no defaults are inserted): {missing}"
    return True, f"{len(critical)} critical fields explicitly supplied"


@rule("DQ04", "ALL", "CRITICAL", "REWORK", "UNIT_INVALID", "numeric specs carry SI-compatible units")
def dq04(ctx: RuleContext):
    units = ctx.manifest.measurement_units
    numeric = {}
    for map_name in (ctx.spec.numeric_maps if ctx.spec else ()):
        for k, v in (ctx.payload.get(map_name) or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric[k] = v
    bad_declared = sorted(k for k, u in units.items() if not is_si_compatible(u))
    missing = sorted(k for k in numeric if k not in units)
    ok_count = len([k for k in numeric if k in units and is_si_compatible(units[k])])
    ctx.metrics["units"] = (ok_count, len(numeric))
    if not numeric and not units:
        return None, "no numeric specs"
    if missing or bad_declared:
        return False, f"numeric specs without units: {missing}; non-SI-compatible units: {bad_declared}"
    return True, f"{len(numeric)} numeric specs carry SI-compatible units"


@rule("DQ05", "ALL", "CRITICAL", "QUARANTINE", "CHECKSUM_MISMATCH",
      "immutable revision and SHA-256 match on raw and output files")
def dq05(ctx: RuleContext):
    m = ctx.manifest
    checks, problems = 0, []
    if ctx.content is None:
        problems.append("artifact bytes not supplied, output_file_sha256 cannot be verified")
    else:
        checks += 1
        if sha256_bytes(ctx.content) != m.output_file_sha256:
            problems.append("output_file_sha256 does not match the artifact bytes")
    for f in (ctx.payload.get("files") or []):
        data = ctx.files.get(f.get("name")) if isinstance(f, dict) else None
        if data is not None:
            checks += 1
            if sha256_bytes(data) != f.get("sha256"):
                problems.append(f"file {f.get('name')} sha256 mismatch")
    sources = ctx.resolved_sources()
    if sources and len(sources) == len(m.source_artifact_ids):
        checks += 1
        if lineage_hash([s.output_sha256 for s in sources]) != m.source_hash:
            problems.append("source_hash does not match the lineage hash of the cited sources")
    existing = ctx.registry.get(m.ref)
    if existing is not None and existing.output_sha256 != m.output_file_sha256:
        problems.append(f"{m.ref} is an immutable released revision with a different hash")
    ctx.metrics["integrity"] = (checks - len([p for p in problems if "mismatch" in p or "does not" in p]), max(checks, 1))
    return (not problems), "; ".join(problems) or f"{checks} checksum(s) verified"


@rule("DQ06", "ALL", "CRITICAL", "BLOCK", "LINEAGE_UNRESOLVED",
      "non-empty source_artifact_ids and evidence_ids resolving to valid upstream DAG nodes")
def dq06(ctx: RuleContext):
    m = ctx.manifest
    problems, resolved = [], 0
    if not m.source_artifact_ids:
        problems.append("source_artifact_ids is empty (root artifacts cite their G0 grant)")
    if not m.evidence_ids:
        problems.append("evidence_ids is empty")
    for ref in m.source_artifact_ids:
        rec = ctx.registry.get(ref)
        if rec is None or rec.tenant_id != ctx.tenant_id:
            problems.append(f"source {ref} does not resolve to a released artifact")
            continue
        check = ctx.handoffs.authorize(rec, m.node_id)
        if not check.ok:
            problems.append(f"source {ref}: {check.reason}")
            continue
        resolved += 1
    for eid in m.evidence_ids:
        if ctx.visible_evidence(eid) is None and ctx.registry.get(eid) is None:
            problems.append(f"evidence {eid} does not resolve")
        else:
            resolved += 1
    ctx.metrics["refs"] = (resolved, len(m.source_artifact_ids) + len(m.evidence_ids))
    return (not problems), "; ".join(problems) or f"{resolved} lineage/evidence refs resolved"


@rule("DQ07", "ALL", "MAJOR", "REWORK", "STALE_DATA", "captured_at within the data-class freshness SLA")
def dq07(ctx: RuleContext):
    sla = FRESHNESS_SECONDS[ctx.spec.data_class if ctx.spec else "document"]
    age = (ctx.now - ctx.manifest.captured_at).total_seconds()
    ctx.metrics["freshness"] = (age, sla)
    if age < -300:
        return False, "captured_at is in the future"
    if age > sla:
        return False, f"captured {age / 86400:.1f} days ago; SLA is {sla / 86400:.1f} days"
    return True, f"age {age / 86400:.2f} days within {sla / 86400:.0f}-day SLA"


@rule("DQ08", "ALL", "CRITICAL", "BLOCK", "RECONCILIATION_FAILED",
      "project/product/revision/MPN reconcile across linked artifacts")
def dq08(ctx: RuleContext):
    m = ctx.manifest
    problems, checks, matched = [], 0, 0
    drift = []
    for ref in m.source_artifact_ids:
        artifact_id, rev = parse_ref(ref)
        cur = ctx.registry.current(artifact_id)
        if cur is not None and cur.revision != rev:
            drift.append(f"{ref} superseded by revision {cur.revision}")
    sources = [s for s in ctx.resolved_sources() if s.schema_key != "rights_grant.v1"]
    primary = sources[0] if sources else next(iter(ctx.resolved_sources()), None)
    if primary is not None:
        checks += 1
        if primary.revision == m.source_revision:
            matched += 1
        else:
            problems.append(f"source_revision {m.source_revision} != primary source revision {primary.revision}")
    product = getattr(ctx.model, "product_id", None)
    proj = ctx.registry.project(m.project_id)
    if product and proj and proj.product_ids:
        checks += 1
        matched += product in proj.product_ids
        if product not in proj.product_ids:
            problems.append(f"product {product} is not registered on project {m.project_id}")
    for s in sources:
        checks += 1
        if s.project_id != m.project_id:
            problems.append(f"{s.ref} belongs to project {s.project_id}")
            continue
        sp = s.facts.get("product_id")
        if product and sp and sp != product:
            problems.append(f"{s.ref} is for product {sp}, not {product}")
            continue
        matched += 1
    bom_rev = getattr(ctx.model, "bom_revision", None)
    boms = [s for s in sources if s.schema_key == "bom_item.v1"]
    if bom_rev and boms:
        checks += 1
        if any(b.facts.get("bom_revision") == bom_rev for b in boms):
            matched += 1
        else:
            problems.append(f"bom_revision {bom_rev} matches no linked BOM")
    mpn = getattr(ctx.model, "mpn", None)
    if mpn and boms:
        checks += 1
        eco = ctx.visible_evidence(getattr(ctx.model, "substitution_eco_id", None) or "")
        known = set().union(*[set(b.facts.get("mpns", [])) | set(b.facts.get("avl_mpns", [])) for b in boms])
        if mpn in known or (eco and eco.attributes.get("to_mpn") == mpn):
            matched += 1
        else:
            problems.append(f"MPN {mpn} is not on the linked BOM revision")
    ctx.metrics["consistency"] = (matched, checks)
    if drift:
        return False, "revision drift: " + "; ".join(drift), "REVISION_DRIFT"
    return (not problems), "; ".join(problems) or f"{matched}/{checks} reconciliation checks matched"


@rule("DQ09", "ALL", "CRITICAL", "BLOCK", "TENANT_ISOLATION", "strict tenant isolation; zero cross-tenant references")
def dq09(ctx: RuleContext):
    m = ctx.manifest
    proj = ctx.registry.project(m.project_id)
    if proj is None or proj.tenant_id != ctx.tenant_id:
        return False, f"project {m.project_id} is not visible to this tenant"
    # artifact IDs are one namespace: an ID already registered elsewhere is never aliased
    if any(r.tenant_id != ctx.tenant_id or r.project_id != m.project_id for r in ctx.registry.revisions(m.artifact_id)):
        return False, f"artifact_id {m.artifact_id} is registered to a different project"
    for ref in m.source_artifact_ids:
        rec = ctx.registry.get(ref)
        if rec is not None and rec.tenant_id != ctx.tenant_id:
            return False, f"source {ref} is not visible to this tenant"
    for eid in m.evidence_ids:
        ev = ctx.registry.evidence(eid)
        if ev is not None and ev.tenant_id != ctx.tenant_id:
            return False, f"evidence {eid} is not visible to this tenant"
    return True, "all references are tenant-local"


@rule("DQ10", "ALL", "CRITICAL", "BLOCK", "SENSOR_DATA_UNTRUSTED",
      "explicit, immutable origin_type; synthetic data never tagged REAL")
def dq10(ctx: RuleContext):
    m = ctx.manifest
    prior = [r for r in ctx.registry.revisions(m.artifact_id) if r.origin_type != m.origin_type]
    if prior:
        return False, f"origin_type is immutable: earlier revisions are {prior[0].origin_type}"
    synthetic_sources = [s.ref for s in ctx.resolved_sources() if s.origin_type == "SYNTHETIC"]
    if m.origin_type in ("REAL", "DERIVED") and synthetic_sources:
        return False, f"derived from synthetic sources {synthetic_sources} but tagged {m.origin_type}"
    if m.origin_type == "REAL":
        sensor = (ctx.payload.get("sensor") or {}) if isinstance(ctx.payload.get("sensor"), dict) else {}
        if sensor.get("status") == "SIMULATED" or ctx.simulator_source:
            return False, "simulated data is tagged REAL"
        if m.tool_kind in AI_KINDS:
            return False, f"{m.tool_kind} output cannot be REAL field data (use DERIVED)"
    return True, f"origin_type {m.origin_type}"


@rule("DQ11", "ALL", "MAJOR", "REWORK", "OUTDATED_EVIDENCE", "LLM factual/engineering claims cite current source_refs")
def dq11(ctx: RuleContext):
    claims = ctx.payload.get("claims")
    if not isinstance(claims, list):
        return None, "no claims"
    bad, cited, needed = [], 0, 0
    for i, c in enumerate(claims):
        if not isinstance(c, dict) or c.get("kind") not in ("factual", "engineering"):
            continue
        needed += 1
        refs = c.get("source_refs") or []
        ok = bool(refs)
        for ref in refs:
            rec = ctx.registry.get(ref)
            if rec is None:
                ok = ok and ctx.visible_evidence(ref) is not None
                continue
            cur = ctx.registry.current(rec.artifact_id)
            if rec.tenant_id != ctx.tenant_id or rec.status != "ACTIVE" or (cur and cur.revision != rec.revision):
                ok = False
        cited += ok
        if not ok:
            bad.append(i)
    ctx.metrics["claims"] = (cited, needed)
    if not needed:
        return None, "no factual/engineering claims"
    return (not bad), f"claims {bad} lack valid, current source_refs" if bad else f"{needed} claims cited"


@rule("DQ12", "ALL", "MAJOR", "REWORK", "MODEL_PROVENANCE_MISSING",
      "model_or_tool, prompt revision and input hashes recorded")
def dq12(ctx: RuleContext):
    m = ctx.manifest
    kind = m.tool_kind
    if kind in {"tool", "engine", "solver"} | AI_KINDS and "@" not in m.model_or_tool:
        return False, f"{m.model_or_tool} has no version"
    if kind in AI_KINDS:
        prov = ctx.ai_provenance or {k: ctx.payload.get(k) for k in ("prompt_revision", "input_hashes")}
        hashes = prov.get("input_hashes") or []
        if not prov.get("prompt_revision") or not hashes or not all(isinstance(h, str) and _SHA.match(h) for h in hashes):
            return False, "AI output needs prompt_revision and SHA-256 input_hashes"
    return True, f"{m.model_or_tool} recorded"


@rule("DQ13", "ALL", "CRITICAL", "BLOCK", "APPROVAL_INVALID",
      "approval claims verified; external side effects need a verified reviewer sign-off")
def dq13(ctx: RuleContext):
    m, appr = ctx.manifest, ctx.approval
    if m.reviewer_id is not None:
        if appr is None or appr.reviewer_id != m.reviewer_id or appr.approved_at != m.approval_timestamp:
            return False, "reviewer_id/approval_timestamp are not backed by a recorded approval"
    if ctx.side_effect:
        if ctx.side_effect not in SIDE_EFFECTS:
            return False, f"unknown side effect {ctx.side_effect}"
        return True, f"{ctx.side_effect} " + (f"approved by {appr.reviewer_id}" if appr else "awaits sign-off (HOLD)")
    return True, "no unverified approval claims"


@rule("DQ26", "ALL", "MAJOR", "REWORK", "UNCALIBRATED_PROBABILITY",
      "risk_score is a probability only for CALIBRATED_ML with calibration evidence")
def dq26(ctx: RuleContext):
    m = ctx.manifest
    if m.risk_method == "CALIBRATED_ML":
        reports = [ctx.visible_evidence(e) for e in m.evidence_ids]
        ok = m.tool_kind == "ml" and any(r and r.kind == "CALIBRATION_REPORT" and _in_date(r, ctx.now) for r in reports)
        return ok, ("calibration evidence present" if ok
                    else "CALIBRATED_ML needs an ml model and in-date CALIBRATION_REPORT evidence; relabelled HEURISTIC")
    if m.risk_method == "SOLVER" and m.tool_kind in LANGUAGE_MODEL_KINDS:
        return False, "a language model is not a solver; relabelled HEURISTIC"
    return True, f"risk_method {m.risk_method}"


@rule("DQ27", "ALL", "MAJOR", "REWORK", "FEEDBACK_NOT_AUTHORIZED",
      "training feedback needs authorization, REAL origin and a human label")
def dq27(ctx: RuleContext):
    m = ctx.manifest
    if not ctx.use_for_training and m.actual_outcome is None:
        return None, "no training feedback"
    problems = []
    if not m.feedback_authorized:
        problems.append("feedback_authorized is false")
    if m.origin_type != "REAL":
        problems.append(f"origin_type {m.origin_type} cannot train models")
    if m.source_license_rights == "APPROVED_NO_TRAIN":
        problems.append("rights are APPROVED_NO_TRAIN")
    labeller = (m.actual_outcome or {}).get("labeled_by")
    appr = ctx.registry.approver(labeller) if labeller else None
    if appr is None or appr.tenant_id != ctx.tenant_id:
        problems.append("actual_outcome.labeled_by is not a registered human reviewer")
    return (not problems), "; ".join(problems) or "feedback authorized for learning"


# --- node-specific rules -------------------------------------------------------------------------

@rule("DQ14", "N1", "MAJOR", "REWORK", "UNSUPPORTED_MARKET_CLAIM", "named buyer account + spend provenance; TAM is not SAM")
def dq14(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "market_opportunity.v1" or ctx.model is None:
        return None, "not a market opportunity"
    mo, problems = ctx.model, []
    if _PLACEHOLDER.match(mo.account_id.strip()) or not mo.account_name.strip():
        problems.append("no named buyer account")
    if mo.basis == "TAM":
        problems.append("TAM is not serviceable demand (SAM)")
    unresolved = [p for p in mo.spend_provenance if ctx.visible_evidence(p) is None and ctx.registry.get(p) is None]
    if not mo.spend_provenance or unresolved:
        problems.append(f"spend provenance missing or unresolved: {unresolved}")
    return (not problems), "; ".join(problems) or f"account {mo.account_id} with evidenced spend"


@rule("DQ15", "N2", "CRITICAL", "BLOCK", "TRACE_INCOMPLETE", "100% critical requirements mapped to verification IDs")
def dq15(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "product_prd.v1" or ctx.model is None:
        return None, "not a PRD"
    critical = [r for r in ctx.model.requirements if r.critical]
    traces = set().union(*[set(t.facts.get("trace", [])) for t in ctx.linked("spec_trace.v1")]) if ctx.linked("spec_trace.v1") else None
    untraced = [r.req_id for r in critical if not r.verification_ids or
                (traces is not None and not all(f"{r.req_id}->{v}" in traces for v in r.verification_ids))]
    ctx.metrics["traced"] = (len(critical) - len(untraced), len(critical))
    if not critical:
        return False, "PRD declares no critical requirements"
    return (not untraced), f"critical requirements without verification mapping: {untraced}" if untraced \
        else f"{len(critical)} critical requirements traced"


@rule("DQ16", "N3", "CRITICAL", "BLOCK", "CRITICAL_DESIGN_RULE", "zero unresolved CRITICAL ERC/DRC findings")
def dq16(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "design_package.v1" or ctx.model is None:
        return None, "not a design package"
    open_ = []
    for f in ctx.model.eda_report.findings:
        if f.severity != "CRITICAL" or f.status == "RESOLVED":
            continue
        waiver = ctx.visible_evidence(f.waiver_evidence_id or "") if f.status == "WAIVED" else None
        if not (waiver and waiver.kind == "WAIVER" and waiver.signed):
            open_.append(f"{f.check}:{f.rule}")
    return (not open_), f"unresolved CRITICAL findings: {open_}" if open_ else "no unresolved CRITICAL ERC/DRC findings"


@rule("DQ17", "N3", "CRITICAL", "REWORK", "NATIVE_DESIGN_MISSING", "native, hash-verified CAD/EDA files present (PDF-only rejected)")
def dq17(ctx: RuleContext):
    files = ctx.payload.get("files")
    if not isinstance(files, list):
        return None, "no file list"
    native = [f for f in files if isinstance(f, dict) and str(f.get("name", "")).lower().endswith(NATIVE_DESIGN_EXT)]
    verified = [f for f in native if f.get("name") in ctx.files and sha256_bytes(ctx.files[f["name"]]) == f.get("sha256")]
    if not native:
        return False, "no native editable design files (PDF/derived exports only)"
    if len(verified) != len(native):
        return False, f"native files not hash-verified: {[f['name'] for f in native if f not in verified]}"
    return True, f"{len(verified)} native design files hash-verified"


@rule("DQ18", "N4", "CRITICAL", "BLOCK", "CAL_EXPIRED", "test instrument calibration valid at captured_at")
def dq18(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "verification_result.v1" or ctx.model is None:
        return None, "not a verification result"
    bad = []
    for inst in ctx.model.instruments:
        ev = ctx.visible_evidence(inst.calibration_id)
        if ev is None or ev.kind != "CALIBRATION" or not _in_date(ev, ctx.manifest.captured_at):
            bad.append(f"{inst.instrument_id}:{inst.calibration_id}")
    known = {i.instrument_id for i in ctx.model.instruments}
    bad += [f"{t.test_id}: unknown instrument {t.instrument_id}" for t in ctx.model.tests
            if t.instrument_id and t.instrument_id not in known]
    return (not bad), f"calibration invalid or expired: {bad}" if bad else "all instruments in calibration"


@rule("DQ19", "N4", "CRITICAL", "BLOCK", "SAFETY_TEST_FAILED", "all safety-critical tests PASS with signed evidence")
def dq19(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "verification_result.v1" or ctx.model is None:
        return None, "not a verification result"
    safety = [t for t in ctx.model.tests if t.safety_critical]
    bad = []
    for t in safety:
        ev = ctx.visible_evidence(t.signed_evidence_id or "")
        if t.result != "PASS" or not (ev and ev.kind == "SIGNED_TEST" and ev.signed):
            bad.append(t.test_id)
    ctx.metrics["traced"] = (len(safety) - len(bad), len(safety))
    return (not bad), f"safety-critical tests not passed with signed evidence: {bad}" if bad \
        else f"{len(safety)} safety-critical tests passed"


@rule("DQ20", "N5", "MAJOR", "REWORK", "QUOTE_STALE", "quote not expired; explicit currency/FX/GST")
def dq20(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "supplier_quote.v1" or ctx.model is None:
        return None, "not a supplier quote"
    q, problems = ctx.model, []
    if q.valid_until < ctx.now.date():
        problems.append(f"quote expired {q.valid_until.isoformat()}")
    if "gst_included" not in ctx.payload:
        problems.append("gst_included not stated explicitly")
    if q.currency != "INR" and not _present(ctx.payload, "fx_rate_to_inr"):
        problems.append(f"{q.currency} quote without an explicit fx_rate_to_inr")
    return (not problems), "; ".join(problems) or f"valid until {q.valid_until.isoformat()}"


@rule("DQ21", "N5", "CRITICAL", "BLOCK", "ALT_NOT_APPROVED", "exact AVL MPN match or signed substitution ECO")
def dq21(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "supplier_quote.v1" or ctx.model is None:
        return None, "not a supplier quote"
    boms = ctx.linked("bom_item.v1")
    if not boms:
        return False, "quote cites no BOM, so the AVL cannot be checked"
    mpn = ctx.model.mpn
    avl = set().union(*[set(b.facts.get("avl_mpns", [])) for b in boms])
    if mpn in avl:
        return True, f"{mpn} is on the AVL"
    eco = ctx.visible_evidence(ctx.model.substitution_eco_id or "")
    lines = set().union(*[set(b.facts.get("mpns", [])) for b in boms])
    if eco and eco.kind == "ECO" and eco.signed and eco.attributes.get("to_mpn") == mpn \
            and eco.attributes.get("from_mpn") in lines:
        return True, f"{mpn} substitutes {eco.attributes['from_mpn']} under signed ECO {eco.evidence_id}"
    return False, f"{mpn} is not on the AVL and has no signed substitution ECO"


@rule("DQ22", "N6", "CRITICAL", "BLOCK", "INELIGIBLE_BID", "contract/tender eligibility PASS (UNKNOWN = FAIL)")
def dq22(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "bid_package.v1" or ctx.model is None:
        return None, "not a bid package"
    return ctx.model.eligibility == "PASS", f"eligibility {ctx.model.eligibility}"


@rule("DQ23", "N7", "CRITICAL", "BLOCK", "COST_RECONCILIATION_FAILED",
      "deterministic Decimal cost reconciliation; GST separated; no language-model arithmetic")
def dq23(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "project_pnl.v1" or ctx.model is None:
        return None, "not a project P&L"
    if ctx.manifest.tool_kind in LANGUAGE_MODEL_KINDS:
        return False, f"{ctx.manifest.model_or_tool} is a language model; authoritative arithmetic must be deterministic"
    p, cent = ctx.model, Decimal("0.01")
    subtotal = (p.lines.bom + p.lines.nre + p.lines.tooling + p.lines.lab + p.lines.finance).quantize(cent, ROUND_HALF_UP)
    gst = (subtotal * p.gst_rate).quantize(cent, ROUND_HALF_UP)
    total = subtotal + gst
    expected = {"subtotal_ex_gst": subtotal, "gst": gst, "total_inc_gst": total}
    wrong = {k: f"declared {getattr(p.declared, k)} vs computed {v}" for k, v in expected.items()
             if getattr(p.declared, k).quantize(cent, ROUND_HALF_UP) != v}
    return (not wrong), f"cost reconciliation failed: {wrong}" if wrong else f"reconciled: total {total} {p.currency}"


@rule("DQ24", "N7", "CRITICAL", "BLOCK", "UNFUNDED_CASH_GAP", "working capital covers peak_cash_gap before PO/bid")
def dq24(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "project_pnl.v1" or ctx.model is None:
        return None, "not a project P&L"
    committing = ctx.model.commitment in ("PO", "BID") or ctx.side_effect in ("PO", "TENDER_BID")
    if not committing:
        return None, "no PO/bid commitment"
    f = ctx.model.funding
    if f is None:
        return False, "commitment declared without a funding plan"
    if f.available_working_capital < f.peak_cash_gap:
        return False, f"peak cash gap {f.peak_cash_gap} exceeds available working capital {f.available_working_capital}"
    return True, "peak cash gap funded"


@rule("DQ25", "N8", "CRITICAL", "BLOCK", "TELEMETRY_INVALID",
      "valid unit/lot IDs, monotonic UTC timestamps, calibrated sensor status")
def dq25(ctx: RuleContext):
    if ctx.manifest.output_schema_key != "lifecycle_event.v1" or ctx.model is None:
        return None, "not a lifecycle event"
    ev, problems = ctx.model, []
    for name in ("unit_id", "lot_id"):
        if not _ID.match(getattr(ev, name)):
            problems.append(f"{name} {getattr(ev, name)!r} is not a valid ID")
    t = ev.event_time
    if t.tzinfo is None or t.utcoffset() != timedelta(0):
        problems.append("event_time is not timezone-aware UTC")
    else:
        key = (ctx.tenant_id, ev.station_id, ev.sensor.sensor_id)
        last = ctx.stream_state.get(key)
        if last is not None and t <= last:
            problems.append(f"non-monotonic timestamp {t.isoformat()} <= {last.isoformat()}")
    origin = ctx.manifest.origin_type
    if origin == "REAL":
        cal = ctx.visible_evidence(ev.sensor.calibration_id or "")
        if ev.sensor.status != "CALIBRATED" or cal is None or cal.kind != "CALIBRATION" or \
                (t.tzinfo is not None and not _in_date(cal, t)):
            problems.append("REAL telemetry needs a CALIBRATED sensor with in-date calibration evidence")
    elif origin == "SYNTHETIC" and ev.sensor.status != "SIMULATED":
        problems.append("SYNTHETIC telemetry must report sensor status SIMULATED")
    return (not problems), "; ".join(problems) or f"unit {ev.unit_id} lot {ev.lot_id} valid"


assert sorted(RULES) == [f"DQ{i:02d}" for i in range(1, 28)], sorted(RULES)
