"""N2 PRD service and its append-only tables."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import DateTime, Index, String, event, select
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from ..db import Base, JSONType, append_only_ddl
from ..errors import N0Error
from ..governance import record_approval, release_handoff
from ..models import (ApprovalRequest, Artifact, EvaluateRequest, HandoffReleaseRequest, PRDPayload, utcnow)
from ..repository import Repository, dump, new_id
from ..rules import canonical_json, normalize_revision, revision_order, sha256_hex
from ..security import Principal
from .models import PRDCreateRequest, PRDRecord, PRDRelease


class N2PrdRow(Base):
    __tablename__ = "n2_prds"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    prd_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[str] = mapped_column(String(16), primary_key=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class N2ReleaseRow(Base):
    __tablename__ = "n2_prd_releases"
    release_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prd_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[str] = mapped_column(String(16), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_n2_release_prd", "tenant_id", "prd_id"),)


N2_APPEND_ONLY_TABLES = ("n2_prds", "n2_prd_releases")
for _name in N2_APPEND_ONLY_TABLES:
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(Base.metadata.tables[_name], "after_create", DDL(_stmt).execute_if(dialect=_dialect))


def _next_revision(existing: list[str]) -> str:
    if not existing:
        return "A"
    last = max(existing, key=revision_order)
    n = revision_order(last) + 1
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


class N2Service:
    def __init__(self, state) -> None:
        self.db, self.store, self.pipeline, self.clock = state.db, state.store, state.pipeline, state.clock
        self.release_listeners: list[Callable[[str, str, str], None]] = []

    def _revisions(self, s, tenant: str, prd_id: str) -> list[PRDRecord]:
        rows = s.scalars(select(N2PrdRow).where(N2PrdRow.tenant_id == tenant, N2PrdRow.prd_id == prd_id))
        return sorted((PRDRecord.model_validate(r.record) for r in rows), key=lambda r: revision_order(r.revision))

    def _releases(self, s, tenant: str, prd_id: str) -> list[PRDRelease]:
        rows = s.scalars(select(N2ReleaseRow).where(N2ReleaseRow.tenant_id == tenant, N2ReleaseRow.prd_id == prd_id)
                         .order_by(N2ReleaseRow.created_at))
        return [PRDRelease.model_validate(r.record) for r in rows]

    def create_prd(self, p: Principal, req: PRDCreateRequest) -> PRDRecord:
        if p.tenant_id != req.tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")
        rev = normalize_revision(req.rfq_revision)
        with self.db.session() as s:
            repo = Repository(s)
            row = repo.get_artifact(p.tenant_id, req.rfq_artifact_id, rev or req.rfq_revision)
            if row is None or row.artifact_type != "RFQ":
                raise N0Error(404, "RFQ_NOT_FOUND", f"{req.rfq_artifact_id}@{req.rfq_revision} is not an admitted RFQ")
            d = repo.latest_decision(p.tenant_id, row.artifact_id, row.revision)
            if d is None or d.decision != "PASS":
                raise N0Error(409, "RFQ_NOT_PASS", f"RFQ decision is {d.decision if d else 'missing'}")
            top = repo.latest_revision(p.tenant_id, row.artifact_id)
            if top is not None and top.revision_order > row.revision_order:
                raise N0Error(409, "SUPERSEDED_REVISION", f"RFQ revision {row.revision} is superseded")
            if not any(m.status == "RELEASED" and "N2" in m.target_nodes
                       for m in repo.releases(p.tenant_id, row.artifact_id, row.revision)):
                raise N0Error(409, "RFQ_NOT_RELEASED_TO_N2", "the RFQ was never released to N2 through N0")
            canon = row.canonical["payload"]
            existing = [r.revision for r in self._revisions(s, p.tenant_id, req.prd_id)]
            synthetic = row.synthetic
        revision = _next_revision(existing)
        payload = PRDPayload(prd_id=req.prd_id, product_name=req.product_name, customer_name=canon["customer_name"],
                             product_category=canon["product_category"], quantity=canon["quantity"],
                             source_rfq=f"{row.artifact_id}@{row.revision}", requirements=canon["requirements"],
                             design_constraints=req.design_constraints, design_scopes=req.design_scopes,
                             test_limits=req.test_limits, compliance_standards=canon.get("compliance_standards", []),
                             authored_by=p.principal_id).model_dump(mode="json")
        art = Artifact(tenant_id=p.tenant_id, project_id=req.project_id, artifact_id=f"PRD-{req.prd_id}",
                       artifact_type="PRD", source_id=req.source_id, processing_purpose="PRODUCT_DEFINITION",
                       release_stage="CONCEPT", revision=revision, synthetic=synthetic,
                       source_sha256=sha256_hex(canonical_json(payload)), payload=payload,
                       source_artifact_ids=[f"{row.artifact_id}@{row.revision}"])
        svc = Principal(principal_id=f"svc-n2@{p.tenant_id}", tenant_id=p.tenant_id, roles=["node_service"],
                        assigned_roles=["service"], nodes=["N2"], clearance=p.clearance, auth_method="internal")
        res = self.pipeline.evaluate(svc, EvaluateRequest(artifact=art, origin_node="N2", requested_destinations=["N3"]))
        rec = PRDRecord(prd_id=req.prd_id, tenant_id=p.tenant_id, revision=revision, n0_artifact_id=art.artifact_id,
                        payload_sha256=res.provenance.payload_sha256, n0_decision=res.decision.decision,
                        n0_decision_id=res.decision.decision_id,
                        violated_codes=sorted({v.code for v in res.decision.violated_rules}),
                        source_rfq=payload["source_rfq"], authored_by=p.principal_id, created_at=self.clock())
        if res.canonical_artifact is not None:                            # admitted by N0
            with self.db.session() as s:
                s.add(N2PrdRow(tenant_id=p.tenant_id, prd_id=req.prd_id, revision=revision, record=dump(rec)))
                Repository(s).append_audit(tenant_id=p.tenant_id, event_type="N2_PRD_CREATED", actor_id=p.principal_id,
                                           actor_roles=p.roles, subject=f"PRD-{req.prd_id}@{revision}",
                                           details={"n0_decision": rec.n0_decision, "source_rfq": rec.source_rfq})
        return rec

    def release_prd(self, p: Principal, prd_id: str, revision: str, reason: str) -> PRDRelease:
        now = self.clock()
        with self.db.session() as s:
            revs = {r.revision: r for r in self._revisions(s, p.tenant_id, prd_id)}
            rec = revs.get(normalize_revision(revision) or revision)
            if rec is None:
                raise N0Error(404, "PRD_NOT_FOUND", f"{prd_id}@{revision} not found in this tenant")
            if rec.authored_by == p.principal_id:
                raise N0Error(403, "FOUR_EYES", "the author of a PRD cannot release it")
            prior = self._releases(s, p.tenant_id, prd_id)
            repo = Repository(s)
            ap = record_approval(repo, p, ApprovalRequest(artifact_id=rec.n0_artifact_id, exact_revision=rec.revision,
                                                          scope="RELEASE", decision="APPROVED", reason=reason),
                                 now, self.store)
            m = release_handoff(repo, p, HandoffReleaseRequest(artifact_id=rec.n0_artifact_id, revision=rec.revision,
                                                               source_node="N2", target_nodes=["N3"],
                                                               approval_id=ap.approval_event.approval_id), now)
            rel = PRDRelease(release_id=new_id("PRL"), tenant_id=p.tenant_id, prd_id=prd_id, prd_revision=rec.revision,
                             n0_artifact_id=rec.n0_artifact_id, payload_sha256=rec.payload_sha256,
                             n0_decision_id=m.decision_id, manifest_id=m.manifest_id, manifest_sha256=m.manifest_sha256,
                             approval_id=ap.approval_event.approval_id,
                             approval_event_hash=ap.approval_event.event_hash, approved_by=p.principal_id,
                             released_at=now, target_nodes=m.target_nodes,
                             supersedes_release_id=prior[-1].release_id if prior else None)
            s.add(N2ReleaseRow(release_id=rel.release_id, tenant_id=p.tenant_id, prd_id=prd_id, revision=rec.revision,
                               record=dump(rel)))
            repo.append_audit(tenant_id=p.tenant_id, event_type="N2_PRD_RELEASED", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"{rec.n0_artifact_id}@{rec.revision}",
                              details={"release_id": rel.release_id, "manifest_id": m.manifest_id,
                                       "approval_id": rel.approval_id})
        for listener in self.release_listeners:                          # N3 baseline lock / invalidation
            listener(p.tenant_id, prd_id, rec.revision)
        return rel

    def releases(self, tenant: str, prd_id: str) -> list[PRDRelease]:
        with self.db.session() as s:
            return self._releases(s, tenant, prd_id)

    def latest_release(self, tenant: str, prd_id: str) -> Optional[PRDRelease]:
        rels = self.releases(tenant, prd_id)
        return max(rels, key=lambda r: revision_order(r.prd_revision)) if rels else None

    def view(self, tenant: str, prd_id: str) -> dict:
        with self.db.session() as s:
            revs = self._revisions(s, tenant, prd_id)
            rels = self._releases(s, tenant, prd_id)
        if not revs:
            raise N0Error(404, "PRD_NOT_FOUND", f"{prd_id} not found in this tenant")
        return {"prd_id": prd_id, "revisions": [dump(r) for r in revs], "releases": [dump(r) for r in rels]}
