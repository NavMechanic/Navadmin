"""Typed persistence helpers over ``app.db`` plus the per-tenant hash chains.

Audit events and approval events are chained per tenant: ``event_hash = SHA-256(prev_hash ||
canonical_json(record))`` with the chain head kept in ``chain_heads`` (row-locked with
``SELECT … FOR UPDATE`` on PostgreSQL). ``verify_chain`` recomputes a tenant's chain end to end.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import (ApprovalRow, ArtifactRow, AuditRow, BlobRow, ChainHeadRow, DecisionRow, FeedbackRow, LineageRow,
                 ModelRunRow, ReleaseRow, SourceRow, VendorResultRow, WorkPackageRow)
from .models import (ApprovalEvent, AuditEventOut, BlobRecord, FileRef, GatewayDecision, ModelRun, ReleaseManifest,
                     SourceRecord, utcnow)
from .rules import VALIDATOR_VERSION, canonical_json, sha256_hex
from .storage import make_uri

GENESIS_HASH = "0" * 64


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def chain_hash(prev_hash: str, record: dict) -> str:
    return sha256_hex(prev_hash.encode() + canonical_json(record))


def dump(model) -> dict:
    return model.model_dump(mode="json")


class Repository:
    def __init__(self, session: Session) -> None:
        self.s = session

    # ------------------------------------------------------------------ sources
    def get_source(self, tenant_id: str, source_id: str) -> Optional[SourceRecord]:
        row = self.s.get(SourceRow, (tenant_id, source_id))
        return SourceRecord.model_validate(row.record) if row else None

    def source_row(self, tenant_id: str, source_id: str) -> Optional[SourceRow]:
        return self.s.get(SourceRow, (tenant_id, source_id))

    def add_source(self, record: SourceRecord) -> None:
        self.s.add(SourceRow(tenant_id=record.tenant_id, source_id=record.source_id, record=dump(record),
                             approval_status=record.approval_status, version=1,
                             registered_by=record.registered_by or "unknown"))
        self.s.flush()

    def update_source(self, record: SourceRecord) -> int:
        row = self.s.get(SourceRow, (record.tenant_id, record.source_id), with_for_update=True)
        row.record, row.approval_status = dump(record), record.approval_status
        row.version += 1
        row.updated_at = utcnow()
        self.s.flush()
        return row.version

    # ------------------------------------------------------------------ blobs
    def get_blob(self, blob_id: str) -> Optional[BlobRow]:
        return self.s.get(BlobRow, blob_id)

    def find_blob(self, tenant_id: str, sha256: str, kind: str = "UPLOAD") -> Optional[BlobRow]:
        return self.s.scalars(select(BlobRow).where(BlobRow.tenant_id == tenant_id, BlobRow.sha256 == sha256,
                                                    BlobRow.kind == kind)).first()

    def add_blob(self, row: BlobRow) -> BlobRow:
        self.s.add(row)
        self.s.flush()
        return row

    @staticmethod
    def blob_record(row: BlobRow) -> BlobRecord:
        return BlobRecord(blob_id=row.blob_id, tenant_id=row.tenant_id, source_id=row.source_id, sha256=row.sha256,
                          size_bytes=row.size_bytes, filename=row.filename, declared_mime=row.declared_mime,
                          sniffed_mime=row.sniffed_mime, status=row.status, scan=row.scan,
                          file_ref=FileRef(uri=make_uri(row.bucket, row.object_key), sha256=row.sha256,
                                           mime=row.sniffed_mime, size_bytes=row.size_bytes, blob_id=row.blob_id),
                          created_at=row.created_at if row.created_at.tzinfo else row.created_at.replace(
                              tzinfo=utcnow().tzinfo))

    # ------------------------------------------------------------------ artifacts
    def get_artifact(self, tenant_id: str, artifact_id: str, revision: str) -> Optional[ArtifactRow]:
        return self.s.scalars(select(ArtifactRow).where(ArtifactRow.tenant_id == tenant_id,
                                                        ArtifactRow.artifact_id == artifact_id,
                                                        ArtifactRow.revision == revision)).first()

    def revisions(self, tenant_id: str, artifact_id: str) -> list[ArtifactRow]:
        return list(self.s.scalars(select(ArtifactRow).where(ArtifactRow.tenant_id == tenant_id,
                                                             ArtifactRow.artifact_id == artifact_id)
                                   .order_by(ArtifactRow.revision_order)))

    def latest_revision(self, tenant_id: str, artifact_id: str) -> Optional[ArtifactRow]:
        revs = self.revisions(tenant_id, artifact_id)
        return revs[-1] if revs else None

    def same_content_elsewhere(self, tenant_id: str, artifact_id: str, payload_sha256: str) -> list[str]:
        rows = self.s.scalars(select(ArtifactRow).where(ArtifactRow.tenant_id == tenant_id,
                                                        ArtifactRow.payload_sha256 == payload_sha256,
                                                        ArtifactRow.artifact_id != artifact_id))
        return sorted({f"{r.artifact_id}@{r.revision}" for r in rows})

    def add_artifact(self, row: ArtifactRow) -> ArtifactRow:
        self.s.add(row)
        self.s.flush()
        return row

    # ------------------------------------------------------------------ decisions
    def latest_decision(self, tenant_id: str, artifact_id: str, revision: str) -> Optional[GatewayDecision]:
        row = self.s.scalars(select(DecisionRow).where(DecisionRow.tenant_id == tenant_id,
                                                       DecisionRow.artifact_id == artifact_id,
                                                       DecisionRow.revision == revision)
                             .order_by(DecisionRow.seq.desc())).first()
        return GatewayDecision.model_validate(row.record) if row else None

    def add_decision(self, d: GatewayDecision) -> None:
        seq = self.s.scalar(select(func.count()).select_from(DecisionRow).where(
            DecisionRow.tenant_id == d.tenant_id, DecisionRow.artifact_id == d.artifact_id,
            DecisionRow.revision == d.revision)) or 0
        self.s.add(DecisionRow(decision_id=d.decision_id, tenant_id=d.tenant_id, artifact_id=d.artifact_id,
                               revision=d.revision, decision=d.decision, record=dump(d),
                               validator_version=d.validator_version, seq=seq + 1))
        self.s.flush()

    # ------------------------------------------------------------------ release manifests
    def add_release(self, m: ReleaseManifest) -> ReleaseManifest:
        body = dump(m)
        body.pop("manifest_sha256", None)
        m = m.model_copy(update={"manifest_sha256": sha256_hex(canonical_json(body))})
        self.s.add(ReleaseRow(manifest_id=m.manifest_id, tenant_id=m.tenant_id, artifact_id=m.artifact_id,
                              revision=m.revision, status=m.status, record=dump(m), manifest_sha256=m.manifest_sha256))
        self.s.flush()
        return m

    def releases(self, tenant_id: str, artifact_id: str, revision: Optional[str] = None) -> list[ReleaseManifest]:
        q = select(ReleaseRow).where(ReleaseRow.tenant_id == tenant_id, ReleaseRow.artifact_id == artifact_id)
        if revision is not None:
            q = q.where(ReleaseRow.revision == revision)
        return [ReleaseManifest.model_validate(r.record) for r in self.s.scalars(q.order_by(ReleaseRow.created_at))]

    # ------------------------------------------------------------------ model runs
    def add_model_run(self, run: ModelRun) -> None:
        self.s.add(ModelRunRow(run_id=run.run_id, tenant_id=run.tenant_id, record=dump(run)))
        self.s.flush()

    def get_model_run(self, tenant_id: str, run_id: str) -> Optional[ModelRun]:
        row = self.s.get(ModelRunRow, run_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return ModelRun.model_validate(row.record)

    # ------------------------------------------------------------------ hash chains
    def _advance_chain(self, tenant_id: str, chain: str, record: dict) -> tuple[str, str]:
        head = self.s.get(ChainHeadRow, (tenant_id, chain), with_for_update=True)
        if head is None:
            head = ChainHeadRow(tenant_id=tenant_id, chain=chain, head_hash=GENESIS_HASH, length=0)
            self.s.add(head)
            self.s.flush()
        prev = head.head_hash
        record = {**record, "prev_hash": prev}
        record.pop("event_hash", None)
        digest = chain_hash(prev, record)
        head.head_hash, head.length = digest, head.length + 1
        return prev, digest

    def append_approval(self, *, tenant_id: str, artifact_id: str, exact_revision: str, approver_id: str,
                        approver_role: str, decision: str, scope: str, reason: str) -> ApprovalEvent:
        body = {"schema_version": "ApprovalEvent.v1", "approval_id": new_id("APV"), "tenant_id": tenant_id,
                "artifact_id": artifact_id, "exact_revision": exact_revision, "approver_id": approver_id,
                "approver_role": approver_role, "decision": decision, "scope": scope, "reason": reason,
                "timestamp_utc": utcnow().isoformat().replace("+00:00", "Z")}
        prev, digest = self._advance_chain(tenant_id, "approval", body)
        event = ApprovalEvent.model_validate({**body, "prev_hash": prev, "event_hash": digest})
        self.s.add(ApprovalRow(approval_id=event.approval_id, tenant_id=tenant_id, artifact_id=artifact_id,
                               exact_revision=exact_revision, scope=scope, decision=decision, approver_id=approver_id,
                               record={**body, "prev_hash": prev, "event_hash": digest}, prev_hash=prev,
                               event_hash=digest))
        self.s.flush()
        return event

    def get_approval(self, tenant_id: str, approval_id: str) -> Optional[ApprovalEvent]:
        row = self.s.get(ApprovalRow, approval_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return ApprovalEvent.model_validate(row.record)

    def approvals_for(self, tenant_id: str, artifact_id: str, revision: Optional[str] = None,
                      scope: Optional[str] = None) -> list[ApprovalEvent]:
        q = select(ApprovalRow).where(ApprovalRow.tenant_id == tenant_id, ApprovalRow.artifact_id == artifact_id)
        if revision is not None:
            q = q.where(ApprovalRow.exact_revision == revision)
        if scope is not None:
            q = q.where(ApprovalRow.scope == scope)
        return [ApprovalEvent.model_validate(r.record) for r in self.s.scalars(q.order_by(ApprovalRow.created_at))]

    def append_audit(self, *, tenant_id: str, event_type: str, actor_id: str, actor_roles: Iterable[str],
                     subject: str, details: dict[str, Any], model_runs: Optional[list[dict]] = None,
                     stage_timings_ms: Optional[dict[str, float]] = None) -> AuditEventOut:
        body = {"event_id": new_id("AUD"), "event_type": event_type, "tenant_id": tenant_id, "actor_id": actor_id,
                "actor_roles": sorted(actor_roles), "subject": subject, "validator_version": VALIDATOR_VERSION,
                "model_runs": model_runs or [], "stage_timings_ms": stage_timings_ms or {},
                "details": details, "created_at": utcnow().isoformat().replace("+00:00", "Z")}
        body = AuditEventOut.model_validate({**body, "prev_hash": GENESIS_HASH, "event_hash": GENESIS_HASH}
                                            ).model_dump(mode="json")
        prev, digest = self._advance_chain(tenant_id, "audit", body)
        body.update(prev_hash=prev, event_hash=digest)
        self.s.add(AuditRow(event_id=body["event_id"], tenant_id=tenant_id, event_type=event_type,
                            subject=subject[:256], actor_id=actor_id, record=body, prev_hash=prev, event_hash=digest))
        self.s.flush()
        return AuditEventOut.model_validate(body)

    def audit_events(self, tenant_id: str, limit: int = 100) -> list[AuditEventOut]:
        rows = self.s.scalars(select(AuditRow).where(AuditRow.tenant_id == tenant_id)
                              .order_by(AuditRow.created_at.desc()).limit(limit))
        return [AuditEventOut.model_validate(r.record) for r in rows]

    def verify_chain(self, tenant_id: str, chain: str) -> dict:
        model = AuditRow if chain == "audit" else ApprovalRow
        rows = list(self.s.scalars(select(model).where(model.tenant_id == tenant_id)))
        by_prev = {r.prev_hash: r for r in rows}
        if len(by_prev) != len(rows):
            return {"chain": chain, "ok": False, "length": len(rows), "error": "fork: two events share a prev_hash"}
        head = self.s.get(ChainHeadRow, (tenant_id, chain))
        expected_head = head.head_hash if head else GENESIS_HASH
        cur, n = GENESIS_HASH, 0
        while cur in by_prev:
            r = by_prev[cur]
            body = dict(r.record)
            stored = body.pop("event_hash")
            if body.get("prev_hash") != cur or chain_hash(cur, body) != stored or stored != r.event_hash:
                return {"chain": chain, "ok": False, "length": len(rows), "error": f"hash mismatch at event {n}"}
            cur, n = stored, n + 1
        ok = n == len(rows) and cur == expected_head
        return {"chain": chain, "ok": ok, "length": n, "head": cur,
                **({} if ok else {"error": "chain does not reach the recorded head or has orphan events"})}

    # ------------------------------------------------------------------ lineage
    def add_edge(self, tenant_id: str, source: str, target: str, relation: str, **attrs: Any) -> None:
        exists = self.s.scalars(select(LineageRow.id).where(LineageRow.tenant_id == tenant_id,
                                                            LineageRow.source == source, LineageRow.target == target,
                                                            LineageRow.relation == relation)).first()
        if exists is None:
            self.s.add(LineageRow(tenant_id=tenant_id, source=source, target=target, relation=relation, attrs=attrs))
            self.s.flush()

    def edges_touching(self, tenant_id: str, node_ids: Iterable[str]) -> list[LineageRow]:
        ids = list(node_ids)
        if not ids:
            return []
        return list(self.s.scalars(select(LineageRow).where(LineageRow.tenant_id == tenant_id,
                                                            (LineageRow.source.in_(ids)) | (LineageRow.target.in_(ids)))))

    # ------------------------------------------------------------------ feedback / vendor
    def add_feedback(self, row: FeedbackRow) -> None:
        self.s.add(row)
        self.s.flush()

    def get_work_package(self, tenant_id: str, wp_id: str) -> Optional[WorkPackageRow]:
        return self.s.get(WorkPackageRow, (tenant_id, wp_id))

    def add_work_package(self, row: WorkPackageRow) -> None:
        self.s.add(row)
        self.s.flush()

    def add_vendor_result(self, row: VendorResultRow) -> None:
        self.s.add(row)
        self.s.flush()
