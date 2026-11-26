"""Human governance: ApprovalEvent.v1, REVIEW resolution, cross-node handoff release, feedback labels.

Invariants enforced here:
* a hard-policy BLOCK is final for its revision — no approval, feedback or score changes it;
* only a REVIEW can be resolved, by a gateway_reviewer who did not submit the artifact;
* a handoff needs the latest decision to be PASS, a RELEASE ApprovalEvent for that exact revision
  issued after that decision by someone other than the submitter, targets within
  ``allowed_destinations`` and a revision that is not superseded;
* feedback can be marked for training only for real (``synthetic=false``) data whose source
  rights include MODEL_TRAINING.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .db import ArtifactRow, FeedbackRow
from .errors import N0Error
from .models import (ApprovalEvent, ApprovalRequest, ApprovalResponse, FeedbackRecord, FeedbackRequest, FileRef,
                     GatewayDecision, HandoffReleaseRequest, ReleaseManifest)
from .pipeline import promote_blobs
from .repository import Repository, dump, new_id
from .rules import (VALIDATOR_VERSION, Findings, allowed_destinations, evaluate_rights, normalize_revision,
                    rights_dependencies)
from .security import Principal
from .storage import ObjectStore


def _ts(v: str | datetime) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def load_current(repo: Repository, tenant_id: str, artifact_id: str, revision: str
                 ) -> tuple[str, ArtifactRow, GatewayDecision]:
    rev = normalize_revision(revision)
    if rev is None:
        raise N0Error(422, "REVISION_FORMAT", f"revision {revision!r} is not A..ZZZ or a number")
    row = repo.get_artifact(tenant_id, artifact_id, rev)
    if row is None:
        raise N0Error(404, "ARTIFACT_NOT_FOUND", f"{artifact_id}@{rev} was not admitted in this tenant")
    decision = repo.latest_decision(tenant_id, artifact_id, rev)
    if decision is None:                                   # pragma: no cover - admitted rows always have one
        raise N0Error(409, "NO_DECISION", f"{artifact_id}@{rev} has no gateway decision")
    return rev, row, decision


def _not_superseded(repo: Repository, row: ArtifactRow) -> None:
    latest = repo.latest_revision(row.tenant_id, row.artifact_id)
    if latest is not None and latest.revision_order > row.revision_order:
        raise N0Error(409, "SUPERSEDED_REVISION", f"{row.artifact_id}@{row.revision} is superseded by "
                                                  f"@{latest.revision}")


def _four_eyes(principal: Principal, row: ArtifactRow) -> None:
    if principal.principal_id == row.submitted_by:
        raise N0Error(403, "FOUR_EYES", "the submitter of an artifact cannot approve or review it")


def _require(principal: Principal, role: str) -> None:
    if not principal.has(role):
        raise N0Error(403, "ROLE_REQUIRED", f"requires role {role}")


def record_approval(repo: Repository, principal: Principal, req: ApprovalRequest, now: datetime,
                    store: Optional[ObjectStore] = None) -> ApprovalResponse:
    rev, row, current = load_current(repo, principal.tenant_id, req.artifact_id, req.exact_revision)
    _require(principal, "release_manager" if req.scope == "RELEASE" else "gateway_reviewer")
    _four_eyes(principal, row)
    _not_superseded(repo, row)
    if current.decision == "BLOCK":
        raise N0Error(409, "BLOCK_NOT_OVERRIDABLE", "a hard-policy BLOCK cannot be approved, resolved or "
                                                    "overridden; fix the cause and submit a new revision",
                      decision_id=current.decision_id)
    if req.scope == "RELEASE" and current.decision != "PASS":
        raise N0Error(409, "NOT_RELEASABLE", f"latest decision is {current.decision}; only PASS can be approved "
                                             "for release", decision_id=current.decision_id)
    if req.scope == "REVIEW_RESOLUTION" and current.decision != "REVIEW":
        raise N0Error(409, "NOTHING_TO_RESOLVE", f"latest decision is {current.decision}, not REVIEW",
                      decision_id=current.decision_id)

    event = repo.append_approval(tenant_id=principal.tenant_id, artifact_id=req.artifact_id, exact_revision=rev,
                                 approver_id=principal.principal_id,
                                 approver_role="release_manager" if req.scope == "RELEASE" else "gateway_reviewer",
                                 decision=req.decision, scope=req.scope, reason=req.reason)
    node = f"artifact:{req.artifact_id}@{rev}"
    repo.add_edge(principal.tenant_id, f"approval:{event.approval_id}", node, "APPROVES" if req.decision ==
                  "APPROVED" else "REJECTS", scope=req.scope)
    if req.scope == "RELEASE":
        return ApprovalResponse(approval_event=event)

    approved = req.decision == "APPROVED"
    outcome = "PASS" if approved else "REWORK"
    destinations = allowed_destinations(outcome, row.artifact_type, row.canonical["processing_purpose"],
                                        row.origin_node, None)
    resolved = current.model_copy(update={
        "decision_id": new_id("DEC"), "decision": outcome, "allowed_destinations": destinations,
        "decided_at": now, "validator_version": VALIDATOR_VERSION, "resolved_by_approval_id": event.approval_id,
        "explanation": f"REVIEW {'resolved' if approved else 'rejected'} by {principal.principal_id} "
                       f"({event.approval_id}): {req.reason}. Original finding: {current.explanation}"})
    repo.add_decision(resolved)
    if approved and store is not None:
        refs = [FileRef.model_validate(r) for r in row.canonical.get("file_refs", [])]
        promoted = promote_blobs(repo, store, row.tenant_id, refs, row.payload_sha256)
        row.canonical = {**row.canonical, "file_refs": [dump(r) for r in promoted]}
    manifest = repo.add_release(ReleaseManifest(
        manifest_id=new_id("REL"), tenant_id=row.tenant_id, artifact_id=row.artifact_id, revision=rev,
        status="RELEASABLE" if approved else "NOT_RELEASED", source_node=row.origin_node,
        allowed_destinations=destinations, source_sha256=row.source_sha256, payload_sha256=row.payload_sha256,
        decision_id=resolved.decision_id, approval_id=event.approval_id, issued_at=now,
        validator_version=VALIDATOR_VERSION))
    repo.add_edge(principal.tenant_id, node, f"decision:{resolved.decision_id}", "EVALUATED_AS",
                  decision=outcome, resolved_by=event.approval_id)
    repo.add_edge(principal.tenant_id, node, f"release:{manifest.manifest_id}", "MANIFEST", status=manifest.status)
    return ApprovalResponse(approval_event=event, resulting_decision=resolved)


def release_handoff(repo: Repository, principal: Principal, req: HandoffReleaseRequest, now: datetime
                    ) -> ReleaseManifest:
    _require(principal, "release_manager")
    rev, row, current = load_current(repo, principal.tenant_id, req.artifact_id, req.revision)
    _not_superseded(repo, row)
    if current.decision != "PASS":
        code = "HARD_POLICY_BLOCK" if current.decision == "BLOCK" else "NOT_RELEASABLE"
        raise N0Error(409, code, f"latest decision for {req.artifact_id}@{rev} is {current.decision}; only PASS "
                                 "can be handed off", decision_id=current.decision_id,
                      violated=[v.code for v in current.violated_rules])
    if req.source_node != row.origin_node:
        raise N0Error(409, "SOURCE_NODE_MISMATCH", f"artifact originated at {row.origin_node}, not {req.source_node}")
    for sid in [row.source_id, *rights_dependencies(row.artifact_type, (row.canonical or {}).get("payload"))]:
        src = repo.get_source(row.tenant_id, sid)
        if src is None or src.approval_status != "APPROVED":
            raise N0Error(409, "SOURCE_RIGHTS_REVOKED", f"{req.artifact_id}@{rev} depends on source {sid}, which is "
                                                        f"{src.approval_status if src else 'not registered'}")
    blocked = [n for n in req.target_nodes if n not in current.allowed_destinations]
    if blocked:
        raise N0Error(409, "DESTINATION_NOT_ALLOWED", f"{blocked} not in allowed_destinations "
                                                      f"{current.allowed_destinations}")
    approval = _release_approval(repo, principal.tenant_id, req.artifact_id, rev, req.approval_id)
    if approval.approver_id == row.submitted_by:            # defence in depth; record_approval also refuses
        raise N0Error(403, "FOUR_EYES", "release approval was issued by the submitter")
    if _ts(approval.timestamp_utc) < _ts(current.decided_at):
        raise N0Error(409, "APPROVAL_STALE", f"approval {approval.approval_id} predates the current decision "
                                             f"{current.decision_id}")
    manifest = repo.add_release(ReleaseManifest(
        manifest_id=new_id("REL"), tenant_id=row.tenant_id, artifact_id=row.artifact_id, revision=rev,
        status="RELEASED", source_node=row.origin_node, allowed_destinations=current.allowed_destinations,
        target_nodes=req.target_nodes, source_sha256=row.source_sha256, payload_sha256=row.payload_sha256,
        decision_id=current.decision_id, approval_id=approval.approval_id, released_by=principal.principal_id,
        issued_at=now, validator_version=VALIDATOR_VERSION))
    node = f"artifact:{row.artifact_id}@{rev}"
    repo.add_edge(row.tenant_id, node, f"release:{manifest.manifest_id}", "RELEASED_VIA", status="RELEASED")
    for target in req.target_nodes:
        repo.add_edge(row.tenant_id, f"release:{manifest.manifest_id}", f"node:{target}", "DELIVERED_TO")
    return manifest


def _release_approval(repo: Repository, tenant_id: str, artifact_id: str, rev: str,
                      approval_id: Optional[str]) -> ApprovalEvent:
    events = repo.approvals_for(tenant_id, artifact_id, rev, "RELEASE")
    if not events:
        raise N0Error(409, "APPROVAL_REQUIRED", f"no RELEASE ApprovalEvent for {artifact_id}@{rev}")
    latest = events[-1]
    if approval_id is not None:
        chosen = next((e for e in events if e.approval_id == approval_id), None)
        if chosen is None:
            raise N0Error(409, "APPROVAL_REQUIRED", f"approval {approval_id} is not a RELEASE approval of "
                                                    f"{artifact_id}@{rev}")
        if chosen.approval_id != latest.approval_id and latest.decision == "REJECTED":
            raise N0Error(409, "APPROVAL_REJECTED", f"a later approval event {latest.approval_id} rejected the "
                                                    "release")
    else:
        chosen = latest
    if chosen.decision != "APPROVED":
        raise N0Error(409, "APPROVAL_REJECTED", f"approval {chosen.approval_id} rejected the release: "
                                                f"{chosen.reason}")
    return chosen


def record_feedback(repo: Repository, principal: Principal, req: FeedbackRequest, now: datetime) -> FeedbackRecord:
    _require(principal, "gateway_reviewer")
    rev, row, current = load_current(repo, principal.tenant_id, req.artifact_id, req.revision)
    eligible = False
    if req.training_label is not None and req.training_label.use_for_training:
        if row.synthetic:
            raise N0Error(422, "SYNTHETIC_NOT_TRAINABLE", "synthetic=true artifacts cannot become training labels")
        source = repo.get_source(row.tenant_id, row.source_id)
        rights = evaluate_rights(row.canonical["processing_purpose"], row.source_id, source, now, Findings())
        if not rights.training_allowed:
            raise N0Error(403, "TRAINING_RIGHTS_MISSING", "source rights do not permit MODEL_TRAINING on real data "
                                                          f"({'; '.join(rights.reasons) or 'MODEL_TRAINING not granted'})")
        eligible = True
    parts = [c.reason for c in req.corrections]
    if req.override_reason:
        parts.append(f"override note: {req.override_reason}")
    if req.training_label:
        parts.append(f"label: {req.training_label.label}")
    reason = ("feedback — " + "; ".join(parts)) if parts else "feedback recorded without comment"
    event = repo.append_approval(tenant_id=principal.tenant_id, artifact_id=req.artifact_id, exact_revision=rev,
                                 approver_id=principal.principal_id, approver_role="gateway_reviewer",
                                 decision="APPROVED", scope="FEEDBACK", reason=reason[:2000])
    record = FeedbackRecord(feedback_id=new_id("FBK"), tenant_id=principal.tenant_id, artifact_id=req.artifact_id,
                            revision=rev, reviewer_id=principal.principal_id, corrections=req.corrections,
                            override_reason=req.override_reason, training_label=req.training_label,
                            training_eligible=eligible, approval_event=event, created_at=now)
    repo.add_feedback(FeedbackRow(feedback_id=record.feedback_id, tenant_id=record.tenant_id,
                                  artifact_id=record.artifact_id, revision=rev,
                                  record={**dump(record), "decision_at_feedback": current.decision},
                                  training_eligible=eligible))
    repo.add_edge(principal.tenant_id, f"approval:{event.approval_id}", f"artifact:{req.artifact_id}@{rev}",
                  "FEEDBACK_ON", feedback_id=record.feedback_id, training_eligible=eligible)
    return record
