"""DQGateway — the deterministic Data Quality Gateway.

Stages, strictly in order, for every node N1–N8 and the shared layer X:

    RIGHTS (G0) → INGEST (ING-01..12) → S01 FEATURE BUNDLE → S02 RISK & ANOMALY
    → S03 HARD POLICY GATE (DQ01..DQ27) → S04 FEASIBLE ACTIONS → HUMAN APPROVAL (G0..G8)
    → VERSIONED RELEASE → S05 EXPLAIN + LEARNING (FB-01..FB-12)

A rights or tenancy failure stops at RIGHTS: the payload is not validated, scored or retained.
``submit`` never raises for bad data — every outcome is a ``GateDecision``.
"""
from __future__ import annotations

import json
import re
import statistics
import threading
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from .handoffs import GATES, NODE_APPROVAL_GATE, HandoffLedger
from .manifest import APPROVED_RIGHTS, CanonicalIOManifest, parse_artifact_uri, sha256_bytes
from .registry import ArtifactRecord, ArtifactRegistry
from .rules import RULES, ApprovalRecord, RuleContext, RuleResult, evaluate_rules
from .schemas import SCHEMAS
from .scorer import SCORE_THRESHOLD, ScoreCard, decide_gate, score
from .units import UnitError, normalize

Transport = Literal["HTTP", "WEBHOOK", "GRAPHQL", "MQTT", "OPCUA", "SFTP", "EMAIL", "UPLOAD", "FILE"]
SideEffect = Literal["PO", "TENDER_BID", "DESIGN_RELEASE", "CERTIFICATION"]

FB_CATALOG = {
    "FB-01": ("Rights / G0 scope", {"DQ01"}),
    "FB-02": ("Schema non-compliance", {"DQ02"}),
    "FB-03": ("Critical field missing", {"DQ03"}),
    "FB-04": ("Unit / SI normalization", {"DQ04"}),
    "FB-05": ("Integrity & lineage", {"DQ05", "DQ06"}),
    "FB-06": ("AI / model governance", {"DQ11", "DQ12", "DQ26", "DQ27"}),
    "FB-07": ("Freshness / stale data", {"DQ07", "DQ20"}),
    "FB-08": ("Isolation & reconciliation", {"DQ08", "DQ09"}),
    "FB-09": ("Origin & telemetry trust", {"DQ10", "DQ25"}),
    "FB-10": ("Engineering & safety", {"DQ15", "DQ16", "DQ17", "DQ18", "DQ19"}),
    "FB-11": ("Commercial, finance & approvals", {"DQ13", "DQ14", "DQ21", "DQ22", "DQ23", "DQ24"}),
    "FB-12": ("Revision invalidation", set()),
}
RULE_TO_FB = {rid: fb for fb, (_, rids) in FB_CATALOG.items() for rid in rids}
SIDE_EFFECT_ACTIONS = {"po", "tender_bid", "design_release", "certification"}
FEASIBLE_BY_GATE = {
    "QUARANTINE": {"quarantine_review", "reject", "request_resubmission"},
    "BLOCK": {"reject", "request_rights", "escalate", "request_resubmission"},
    "REWORK": {"rework", "reject", "request_resubmission"},
    "HOLD": {"approve", "reject", "rework"},
    "RELEASED": {"publish", "handoff", "retrain"} | SIDE_EFFECT_ACTIONS,
}
DEFAULT_ACTION = {"QUARANTINE": "quarantine_review", "BLOCK": "reject", "REWORK": "rework", "HOLD": "approve",
                  "RELEASED": "publish"}
TELEMETRY_RULES = {"DQ01", "DQ03", "DQ04", "DQ09", "DQ10", "DQ25"}
_CONNECTOR = re.compile(r"^CON-0(0[1-9]|[1-3][0-9]|4[0-7])$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactSubmission(BaseModel):
    """What a producer hands the gateway: the manifest (raw — validated here) plus payload and bytes."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    manifest: dict[str, Any]
    payload: dict[str, Any] = {}
    content: Optional[bytes] = None              # the artifact's own bytes (verified against output_file_sha256)
    files: dict[str, bytes] = {}                 # native files listed in payload.files (DQ05/DQ17)
    required_critical: list[str] = []            # extra critical paths beyond the schema's own
    side_effect: Optional[SideEffect] = None     # PO / TENDER_BID / DESIGN_RELEASE / CERTIFICATION
    use_for_training: bool = False
    ai_provenance: Optional[dict[str, Any]] = None
    transport: Transport = "HTTP"
    simulator_source: bool = False               # set by trusted infrastructure (e.g. a simulated broker)


@dataclass
class ExceptionRecord:
    exception_id: str
    fb_code: str
    title: str
    tenant_id: str
    event_id: str
    artifact_ref: Optional[str]
    node_id: Optional[str]
    rule_ids: list[str]
    message: str
    created_at: datetime
    open: bool = True
    resolved_by: Optional[str] = None


@dataclass
class GateDecision:
    event_id: str
    tenant_id: str
    actor_id: str
    node_id: Optional[str]
    artifact_ref: Optional[str]
    output_schema: Optional[str]
    origin_type: Optional[str]
    quality_gate: str
    reasons: list[str]
    weighted_score: Optional[float]
    dimensions: dict[str, float]
    rule_results: list[RuleResult]
    stages: dict[str, Any]
    feasible_actions: list[dict]
    recommended_action: Optional[str]
    exceptions: list[str]
    evaluated_manifest: Optional[dict]
    ingest_route: Optional[str]
    decided_at: datetime
    approval: Optional[dict] = None
    released_ref: Optional[str] = None

    @property
    def failed_rules(self) -> list[RuleResult]:
        return [r for r in self.rule_results if r.applicable and not r.passed]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decided_at"] = self.decided_at.isoformat()
        d["failed_rules"] = [r.rule_id for r in self.failed_rules]
        return json.loads(json.dumps(d, default=str))


@dataclass
class ConnectorAuthorization:
    ok: bool
    connector_id: str
    project_id: str
    reasons: list[str]
    no_train: bool = False
    ticket: Optional[str] = None


class DQGatewayError(Exception):
    pass


def ingest_route(connector_mode: str, transport: str, tool_kind: str) -> str:
    """Map the connector mode / transport to the reference ingestion route ING-01..ING-12."""
    if transport == "MQTT":
        return "ING-12"
    if transport == "OPCUA":
        return "ING-11"
    if transport == "WEBHOOK":
        return "ING-03"
    if transport == "GRAPHQL":
        return "ING-02"
    if transport == "EMAIL":
        return "ING-09"
    if transport == "SFTP":
        return "ING-06"
    return {"API": "ING-01", "MCP": "ING-04", "SDK": "ING-05", "CLI": "ING-05", "BATCH": "ING-10",
            "PORTAL": "ING-08" if tool_kind == "vendor" else "ING-07", "MANUAL": "ING-08"}[connector_mode]


def _split_errors(exc: ValidationError) -> tuple[list[str], list[str]]:
    missing, schema = [], []
    for e in exc.errors():
        path = ".".join(str(p) for p in e["loc"])
        (missing if e["type"] == "missing" else schema).append(path if e["type"] == "missing" else f"{path}: {e['msg']}")
    return missing, schema


def _fingerprint(sub: ArtifactSubmission, tenant_id: str) -> str:
    body = {"tenant": tenant_id, "manifest": sub.manifest, "payload": sub.payload,
            "content": sha256_bytes(sub.content) if sub.content is not None else None,
            "files": {k: sha256_bytes(v) for k, v in sorted(sub.files.items())},
            "side_effect": sub.side_effect, "training": sub.use_for_training}
    return sha256_bytes(json.dumps(body, sort_keys=True, default=str).encode())


class DQGateway:
    def __init__(self, registry: Optional[ArtifactRegistry] = None, clock: Callable[[], datetime] = utcnow,
                 score_threshold: float = SCORE_THRESHOLD) -> None:
        self.registry = registry or ArtifactRegistry()
        self.handoffs = HandoffLedger(self.registry)
        self.clock = clock
        self.threshold = score_threshold
        self._lock = threading.RLock()
        self._decisions: dict[str, GateDecision] = {}
        self._fingerprints: dict[str, str] = {}
        self._submissions: dict[str, tuple[ArtifactSubmission, str, str]] = {}
        self._approvals: dict[str, ApprovalRecord] = {}
        self._stream_state: dict = {}
        self._history: dict[tuple, list[float]] = defaultdict(list)
        self.exceptions: list[ExceptionRecord] = []
        self.learning_set: list[dict] = []
        self.telemetry_stats: Counter = Counter()
        self.telemetry_rejections: deque = deque(maxlen=200)

    # ------------------------------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------------------------------
    def submit(self, submission: ArtifactSubmission, tenant_id: str, actor_id: str) -> GateDecision:
        with self._lock:
            fp = _fingerprint(submission, tenant_id)
            event_id = str(submission.manifest.get("event_id", ""))
            if event_id in self._decisions:
                if self._fingerprints.get(event_id) == fp and self._decisions[event_id].tenant_id == tenant_id:
                    return self._decisions[event_id]  # idempotent replay
                return self._bare(event_id or str(uuid.uuid4()), tenant_id, actor_id, "QUARANTINE",
                                  ["event_id already used for a different submission (idempotency violation)"],
                                  store=False)
            decision = self._evaluate(submission, tenant_id, actor_id,
                                      approval=self._recorded_approval(submission, tenant_id))
            self._decisions[decision.event_id] = decision
            self._fingerprints[decision.event_id] = fp
            if decision.stages.get("rights", {}).get("stopped"):
                submission = submission.model_copy(update={"content": None, "files": {}, "payload": {}})
            self._submissions[decision.event_id] = (submission, tenant_id, actor_id)
            return decision

    def _recorded_approval(self, sub: ArtifactSubmission, tenant_id: str) -> Optional[ApprovalRecord]:
        """Approval already recorded in the trusted registry for this exact release.

        Lets another process sharing the registry (CLI ingest, a worker) re-present a RELEASED
        artifact without a second sign-off. It applies only to the same event, tenant and bytes of
        an ACTIVE record whose approver still holds the gate; anything claimed in the submission
        itself is ignored, and every rule is still re-evaluated.
        """
        m = sub.manifest
        try:
            _, artifact_id, rev = parse_artifact_uri(str(m.get("output_artifact_uri", "")))
        except ValueError:
            return None
        rec = self.registry.get(f"{artifact_id}@{rev}")
        if (rec is None or rec.status != "ACTIVE" or rec.tenant_id != tenant_id or not rec.approved_by
                or rec.event_id != str(m.get("event_id")) or rec.output_sha256 != m.get("output_file_sha256")):
            return None
        appr = self.registry.approver(rec.approved_by)
        if appr is None or appr.tenant_id != tenant_id or rec.approval_gate not in appr.gates:
            return None
        return ApprovalRecord(rec.event_id, rec.approved_by, rec.approval_gate,
                              rec.approved_at or rec.registered_at, "recorded release (registry)")

    def _bare(self, event_id, tenant_id, actor_id, gate, reasons, results=None, store=True) -> GateDecision:
        d = GateDecision(event_id, tenant_id, actor_id, None, None, None, None, gate, reasons, None, {},
                         results or [], {}, [], DEFAULT_ACTION.get(gate), [], None, None, self.clock())
        d.exceptions = self._raise_exceptions(d)
        if store:
            self._decisions[event_id] = d
        return d

    def _evaluate(self, sub: ArtifactSubmission, tenant_id: str, actor_id: str,
                  approval: Optional[ApprovalRecord]) -> GateDecision:
        now = self.clock()
        stages: dict[str, Any] = {}
        # --- RIGHTS (G0): the manifest must parse before anything else is trusted --------------------
        try:
            manifest = CanonicalIOManifest.model_validate(sub.manifest)
        except ValidationError as exc:
            missing, schema = _split_errors(exc)
            r = RuleResult("DQ02", "CRITICAL", False, "QUARANTINE", "SCHEMA_INVALID",
                           "manifest invalid: " + "; ".join((schema + [f"{m}: missing" for m in missing])[:6]))
            return self._bare(str(sub.manifest.get("event_id") or uuid.uuid4()), tenant_id, actor_id, "QUARANTINE",
                              ["Canonical I-O Manifest failed validation"], [r], store=False)
        spec = SCHEMAS.get(manifest.output_schema_key)
        ctx = RuleContext(manifest=manifest, payload=sub.payload, model=None, schema_errors=[], missing_errors=[],
                          spec=spec, content=sub.content, files=sub.files, required_critical=tuple(sub.required_critical),
                          tenant_id=tenant_id, actor_id=actor_id, registry=self.registry, handoffs=self.handoffs,
                          now=now, side_effect=sub.side_effect, use_for_training=sub.use_for_training,
                          ai_provenance=sub.ai_provenance, approval=approval, stream_state=self._stream_state,
                          simulator_source=sub.simulator_source)
        rights = evaluate_rules(ctx, {"DQ01", "DQ09"})
        stages["rights"] = {"results": [r.rule_id for r in rights if not r.passed], "stopped": False}
        if any(not r.passed for r in rights):
            stages["rights"]["stopped"] = True
            d = self._decision(manifest, tenant_id, actor_id, "BLOCK",
                               ["No Rights, No Access: stopped at G0 before ingest"], None, rights, stages, [], None,
                               None, now)
            d.exceptions = self._raise_exceptions(d)
            return d

        # --- INGEST (ING-01..12): route + schema validation --------------------------------------------
        route = ingest_route(manifest.connector_mode, sub.transport, manifest.tool_kind)
        if spec is not None:
            try:
                ctx.model = spec.model.model_validate(sub.payload)
            except ValidationError as exc:
                ctx.missing_errors, ctx.schema_errors = _split_errors(exc)
        stages["ingest"] = {"route": route, "schema": manifest.output_schema_key, "payload_valid": ctx.model is not None,
                            "content_bytes": len(sub.content) if sub.content is not None else None}

        # --- S01 feature bundle -----------------------------------------------------------------------
        si_values, unit_errors = {}, {}
        for map_name in (spec.numeric_maps if spec else ()):
            for k, v in (sub.payload.get(map_name) or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and k in manifest.measurement_units:
                    try:
                        si_values[k] = dict(zip(("value", "unit"), normalize(v, manifest.measurement_units[k])))
                    except UnitError as exc:
                        unit_errors[k] = str(exc)
        stages["S01_feature_bundle"] = {
            "si_values": si_values, "unit_errors": unit_errors,
            "age_seconds": round((now - manifest.captured_at).total_seconds(), 1),
            "source_count": len(manifest.source_artifact_ids), "evidence_count": len(manifest.evidence_ids),
            "file_count": len(sub.payload.get("files") or []), "origin_type": manifest.origin_type,
        }

        # --- S02 risk & anomaly -----------------------------------------------------------------------
        anomalies = []
        for k, sv in si_values.items():
            hist = self._history.get((tenant_id, manifest.output_schema_key, k), [])
            if len(hist) >= 8:
                med = statistics.median(hist)
                mad = statistics.median(abs(x - med) for x in hist) or 1e-12
                z = 0.6745 * (sv["value"] - med) / mad
                if abs(z) > 3.5:
                    anomalies.append({"field": k, "robust_z": round(z, 2), "median": med})
        stages["S02_risk_anomaly"] = {"risk_method_declared": manifest.risk_method, "risk_score": manifest.risk_score,
                                      "anomalies": anomalies, "reference": "released REAL history only"}

        # --- S03 hard policy gate ------------------------------------------------------------------------
        results = evaluate_rules(ctx)
        card: ScoreCard = score(ctx)
        hard = dict(manifest.hard_policy_results)
        hard.update({r.rule_id: ("PASS" if r.passed else "FAIL") for r in results
                     if r.applicable and r.severity == "CRITICAL"})
        approval_ok = approval is not None and not [r for r in results if r.rule_id == "DQ13" and not r.passed]
        gate, reasons = decide_gate(results, manifest.hard_policy_results, card.weighted_score, approval_ok, self.threshold)
        stages["S03_hard_policy"] = {"failed": [r.rule_id for r in results if r.applicable and not r.passed],
                                     "weighted_score": card.weighted_score, "threshold": self.threshold}
        risk_method = manifest.risk_method
        if any(r.rule_id == "DQ26" and r.applicable and not r.passed for r in results):
            risk_method = "HEURISTIC"
        stages["S02_risk_anomaly"]["risk_method_effective"] = risk_method

        # --- S04 feasible actions --------------------------------------------------------------------------
        feasible, recommended = self._feasible_actions(manifest, gate, results)
        stages["S04_actions"] = {"candidates": len(manifest.candidate_actions), "feasible": len(feasible)}

        evaluated = manifest.model_copy(update={
            "quality_gate": gate, "hard_policy_results": hard, "risk_method": risk_method,
            "reviewer_id": approval.reviewer_id if gate == "RELEASED" and approval else None,
            "approval_timestamp": approval.approved_at if gate == "RELEASED" and approval else None,
        })
        d = self._decision(manifest, tenant_id, actor_id, gate, reasons, card, results, stages, feasible, recommended,
                           route, now)
        d.evaluated_manifest = json.loads(evaluated.model_dump_json())
        if approval is not None:
            d.approval = {"reviewer_id": approval.reviewer_id, "gate": approval.gate,
                          "approved_at": approval.approved_at.isoformat(), "notes": approval.notes}

        # --- VERSIONED RELEASE -------------------------------------------------------------------------------
        invalidated: list[tuple[str, str]] = []
        if gate == "RELEASED":
            rec = ArtifactRecord(
                artifact_id=manifest.artifact_id, revision=manifest.revision, tenant_id=tenant_id,
                project_id=manifest.project_id, node_id=manifest.node_id, schema_key=manifest.output_schema_key,
                origin_type=manifest.origin_type, rights=manifest.source_license_rights,
                output_sha256=manifest.output_file_sha256, sources=list(manifest.source_artifact_ids),
                event_id=str(manifest.event_id), approved_by=approval.reviewer_id if approval else None,
                approval_gate=approval.gate if approval else None,
                approved_at=approval.approved_at if approval else None,
                facts=spec.facts(ctx.model) if spec and ctx.model is not None else {})
            self.registry.register(rec)
            d.released_ref = rec.ref
            for ref in manifest.source_artifact_ids:
                src = self.registry.get(ref)
                if src is not None:
                    self.handoffs.record(self.handoffs.authorize(src, manifest.node_id).edge_id, ref, rec.ref)
            invalidated = self.handoffs.on_release(rec)
            self._resolve_exceptions(tenant_id, manifest.artifact_id, d.event_id)
            if manifest.origin_type == "REAL":
                for k, sv in si_values.items():
                    self._history[(tenant_id, manifest.output_schema_key, k)].append(sv["value"])
        if gate not in ("BLOCK", "QUARANTINE") and ctx.model is not None and spec and spec.name == "lifecycle_event.v1":
            key = (tenant_id, ctx.model.station_id, ctx.model.sensor.sensor_id)
            self._stream_state[key] = ctx.model.event_time

        # --- S05 explain + learning ------------------------------------------------------------------------------
        d.exceptions = self._raise_exceptions(d)
        for ref, reason in invalidated:
            d.exceptions.append(self._exception("FB-12", tenant_id, d.event_id, ref, None, [], reason).exception_id)
        dq27 = next((r for r in results if r.rule_id == "DQ27"), None)
        learned = gate == "RELEASED" and sub.use_for_training and dq27 is not None and dq27.applicable and dq27.passed
        if learned:
            self.learning_set.append({"event_id": d.event_id, "ref": d.released_ref,
                                      "actual_outcome": manifest.actual_outcome})
        stages["S05_explain"] = {
            "summary": reasons,
            "failed": [{"rule": r.rule_id, "severity": r.severity, "action": r.action_on_fail, "code": r.error_code,
                        "message": r.message} for r in sorted(d.failed_rules, key=lambda r: (r.severity != "CRITICAL",
                                                                                             r.rule_id))],
            "invalidated": [ref for ref, _ in invalidated], "learning_set_appended": learned,
        }
        return d

    def _decision(self, manifest, tenant_id, actor_id, gate, reasons, card, results, stages, feasible, recommended,
                  route, now) -> GateDecision:
        return GateDecision(
            event_id=str(manifest.event_id), tenant_id=tenant_id, actor_id=actor_id, node_id=manifest.node_id,
            artifact_ref=manifest.ref, output_schema=manifest.output_schema_key, origin_type=manifest.origin_type,
            quality_gate=gate, reasons=reasons, weighted_score=card.weighted_score if card else None,
            dimensions=card.dimensions if card else {}, rule_results=results, stages=stages,
            feasible_actions=feasible, recommended_action=recommended or DEFAULT_ACTION.get(gate), exceptions=[],
            evaluated_manifest=None, ingest_route=route, decided_at=now)

    def _feasible_actions(self, manifest: CanonicalIOManifest, gate: str, results: list[RuleResult]):
        allowed = set(FEASIBLE_BY_GATE.get(gate, set()))
        dq27 = next((r for r in results if r.rule_id == "DQ27"), None)
        if not (dq27 and dq27.applicable and dq27.passed):
            allowed.discard("retrain")
        consequence = manifest.expected_cost_inr or Decimal("0")
        out = []
        for cand in manifest.candidate_actions:
            action = str(cand.get("action", "")).lower()
            try:
                cost = Decimal(str(cand.get("cost_inr", "0")))
                residual = Decimal(str(cand.get("residual_risk", manifest.risk_score)))
            except InvalidOperation:
                out.append({"action": action, "feasible": False, "reason": "non-numeric cost or risk"})
                continue
            feasible = action in allowed
            out.append({"action": action, "feasible": feasible,
                        "reason": "" if feasible else f"not permitted while gate is {gate}",
                        "expected_cost_inr": str((cost + residual * consequence).quantize(Decimal("0.01")))})
        ranked = sorted((a for a in out if a["feasible"]), key=lambda a: Decimal(a["expected_cost_inr"]))
        return out, (ranked[0]["action"] if ranked else DEFAULT_ACTION.get(gate))

    # ------------------------------------------------------------------------------------------
    # human approval
    # ------------------------------------------------------------------------------------------
    def approve(self, event_id: str, reviewer_id: str, tenant_id: str, notes: str = "") -> GateDecision:
        with self._lock:
            d = self._decisions.get(event_id)
            if d is None or d.tenant_id != tenant_id:
                raise DQGatewayError(f"decision {event_id} not found")
            if d.quality_gate != "HOLD":
                raise DQGatewayError(f"only HOLD decisions can be approved; {event_id} is {d.quality_gate}")
            sub, sub_tenant, actor = self._submissions[event_id]
            gate_needed = NODE_APPROVAL_GATE.get(d.node_id or "X", "G2")
            appr = self.registry.approver(reviewer_id)
            if appr is None or appr.tenant_id != tenant_id or gate_needed not in appr.gates:
                raise DQGatewayError(f"{reviewer_id} is not an authorized {gate_needed} approver for this tenant")
            if reviewer_id == actor:
                raise DQGatewayError("four-eyes rule: the submitter cannot approve their own artifact")
            record = ApprovalRecord(event_id, reviewer_id, gate_needed, self.clock(), notes)
            new = self._evaluate(sub, sub_tenant, actor, approval=record)
            if new.quality_gate == "RELEASED":
                self._approvals[event_id] = record
            self._decisions[event_id] = new
            return new

    # ------------------------------------------------------------------------------------------
    # connector access (Policy 1: No Rights, No Access)
    # ------------------------------------------------------------------------------------------
    def authorize_connector(self, connector_id: str, project_id: str, artifact_refs: list[str],
                            tenant_id: str) -> ConnectorAuthorization:
        reasons = []
        if not _CONNECTOR.match(connector_id or ""):
            reasons.append(f"{connector_id} is not a registered connector (CON-001..CON-047)")
        proj = self.registry.project(project_id)
        if proj is None or proj.tenant_id != tenant_id:
            return ConnectorAuthorization(False, connector_id, project_id, reasons + ["project not visible to tenant"])
        grant = self.registry.rights_grant(project_id)
        if grant is None or grant.status != "ACTIVE":
            reasons.append("G0: no active rights/NDA grant for the project")
        no_train = False
        if not artifact_refs:
            reasons.append("no artifacts requested")
        for ref in artifact_refs:
            rec = self.registry.get(ref)
            if rec is None or rec.tenant_id != tenant_id:
                reasons.append(f"{ref} is not a released artifact of this tenant")
                continue
            if rec.status != "ACTIVE":
                reasons.append(f"{ref} is {rec.status}")
            if rec.rights not in APPROVED_RIGHTS:
                reasons.append(f"{ref} rights are {rec.rights}")
            no_train |= rec.rights == "APPROVED_NO_TRAIN"
            for rfq in self._lineage(rec, "customer_rfq.v1"):
                if rfq.facts.get("consent_scope") != "VENDOR_ALLOWED":
                    reasons.append(f"{ref} derives from {rfq.ref} whose consent_scope is "
                                   f"{rfq.facts.get('consent_scope', 'unset')}")
        ok = not reasons
        return ConnectorAuthorization(ok, connector_id, project_id, reasons, no_train,
                                      f"{connector_id}-{uuid.uuid4().hex[:12]}" if ok else None)

    def _lineage(self, rec: ArtifactRecord, schema: str) -> list[ArtifactRecord]:
        out, seen, stack = [], set(), [rec]
        while stack:
            cur = stack.pop()
            if cur.ref in seen:
                continue
            seen.add(cur.ref)
            if cur.schema_key == schema:
                out.append(cur)
            stack.extend(s for s in (self.registry.get(r) for r in cur.sources) if s is not None)
        return out

    # ------------------------------------------------------------------------------------------
    # telemetry fast path (DQ01, DQ03, DQ04, DQ09, DQ10, DQ25)
    # ------------------------------------------------------------------------------------------
    def evaluate_telemetry(self, sub: ArtifactSubmission, tenant_id: str) -> tuple[bool, list[RuleResult], Optional[Any]]:
        with self._lock:
            try:
                manifest = CanonicalIOManifest.model_validate(sub.manifest)
            except ValidationError as exc:
                return False, [RuleResult("DQ02", "CRITICAL", False, "QUARANTINE", "SCHEMA_INVALID", str(exc)[:300])], None
            spec = SCHEMAS["lifecycle_event.v1"]
            ctx = RuleContext(manifest=manifest, payload=sub.payload, model=None, schema_errors=[], missing_errors=[],
                              spec=spec, content=None, files={}, required_critical=tuple(sub.required_critical),
                              tenant_id=tenant_id, actor_id="telemetry", registry=self.registry, handoffs=self.handoffs,
                              now=self.clock(), stream_state=self._stream_state, simulator_source=sub.simulator_source)
            try:
                ctx.model = spec.model.model_validate(sub.payload)
            except ValidationError as exc:
                ctx.missing_errors, ctx.schema_errors = _split_errors(exc)
            results = evaluate_rules(ctx, TELEMETRY_RULES)
            if ctx.schema_errors:
                results.append(RuleResult("DQ02", "CRITICAL", False, "QUARANTINE", "SCHEMA_INVALID",
                                          "; ".join(ctx.schema_errors[:3])))
            admitted = all(r.passed for r in results)
            station = sub.payload.get("station_id", "?")
            self.telemetry_stats["admitted" if admitted else "rejected"] += 1
            self.telemetry_stats[f"{station}:{'admitted' if admitted else 'rejected'}"] += 1
            if admitted and ctx.model is not None:
                self._stream_state[(tenant_id, ctx.model.station_id, ctx.model.sensor.sensor_id)] = ctx.model.event_time
            else:
                failed = [r for r in results if not r.passed]
                for r in failed:
                    self.telemetry_stats[f"rule:{r.rule_id}"] += 1
                self.telemetry_rejections.append({"at": self.clock().isoformat(), "station": station,
                                                  "rules": [r.rule_id for r in failed],
                                                  "messages": [r.message for r in failed]})
            return admitted, results, ctx.model

    # ------------------------------------------------------------------------------------------
    # exceptions, queries, stats
    # ------------------------------------------------------------------------------------------
    def _exception(self, fb, tenant_id, event_id, ref, node, rule_ids, message) -> ExceptionRecord:
        rec = ExceptionRecord(f"EXC-{uuid.uuid4().hex[:10]}", fb, FB_CATALOG[fb][0], tenant_id, event_id, ref, node,
                              rule_ids, message, self.clock())
        self.exceptions.append(rec)
        return rec

    def _raise_exceptions(self, d: GateDecision) -> list[str]:
        by_fb: dict[str, list[RuleResult]] = defaultdict(list)
        for r in d.failed_rules:
            by_fb[RULE_TO_FB.get(r.rule_id, "FB-11")].append(r)
        return [self._exception(fb, d.tenant_id, d.event_id, d.artifact_ref, d.node_id, [r.rule_id for r in rs],
                                "; ".join(r.message for r in rs)[:500]).exception_id for fb, rs in sorted(by_fb.items())]

    def _resolve_exceptions(self, tenant_id: str, artifact_id: str, by_event: str) -> None:
        for e in self.exceptions:
            if e.open and e.tenant_id == tenant_id and e.artifact_ref and e.artifact_ref.split("@")[0] == artifact_id:
                e.open, e.resolved_by = False, by_event

    def decision(self, event_id: str, tenant_id: str) -> Optional[GateDecision]:
        d = self._decisions.get(event_id)
        return d if d is not None and d.tenant_id == tenant_id else None

    def decisions(self, tenant_id: str, gate: Optional[str] = None) -> list[GateDecision]:
        return [d for d in self._decisions.values() if d.tenant_id == tenant_id and (gate is None or d.quality_gate == gate)]

    def open_exceptions(self, tenant_id: str) -> list[ExceptionRecord]:
        return [e for e in self.exceptions if e.tenant_id == tenant_id and e.open]

    def stats(self, tenant_id: str) -> dict:
        ds = self.decisions(tenant_id)
        counts = Counter(d.quality_gate for d in ds)
        nodes = {}
        for node in ("N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "X"):
            nd = [d for d in ds if d.node_id == node]
            scored = [d for d in nd if d.weighted_score is not None]
            nodes[node] = {
                "events": len(nd), **{g.lower(): sum(d.quality_gate == g for d in nd)
                                      for g in ("RELEASED", "HOLD", "REWORK", "BLOCK", "QUARANTINE")},
                "avg_score": round(statistics.mean(d.weighted_score for d in scored), 2) if scored else None,
                "release_rate": round(sum(d.quality_gate == "RELEASED" for d in nd) / len(nd), 3) if nd else None,
            }
        return {"gates": {g: counts.get(g, 0) for g in ("RELEASED", "HOLD", "REWORK", "BLOCK", "QUARANTINE")},
                "nodes": nodes, "open_exceptions": len(self.open_exceptions(tenant_id)),
                "telemetry": {k: v for k, v in self.telemetry_stats.items()},
                "learning_set": len(self.learning_set), "approval_gates": GATES}
