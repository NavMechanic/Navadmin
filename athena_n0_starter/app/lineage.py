"""GET /v1/artifacts/{id}/lineage — tenant-scoped provenance graph.

Starting from every admitted revision of the artifact (or one revision), the graph follows
``lineage_edges`` *upstream* (sources, uploaded files, model runs, upstream artifacts, previous
revisions, approvals) and *downstream* (decisions, manifests, releases, receiving nodes, vendor
work packages and their results, later revisions and dependent artifacts). Traversal is directed in
each direction, so siblings — other artifacts that merely share a source — are not pulled in.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from .db import (ApprovalRow, ArtifactRow, BlobRow, DecisionRow, ModelRunRow, ReleaseRow, SourceRow,
                 VendorResultRow, WorkPackageRow)
from .models import LineageEdge, LineageGraph, LineageNode
from .repository import Repository


def _kind(node_id: str) -> str:
    return node_id.split(":", 1)[0]


def _attrs(repo: Repository, tenant_id: str, node_id: str) -> dict:
    kind, ref = node_id.split(":", 1)
    s = repo.s
    if kind == "artifact":
        aid, rev = ref.rsplit("@", 1)
        row = s.scalars(select(ArtifactRow).where(ArtifactRow.tenant_id == tenant_id, ArtifactRow.artifact_id == aid,
                                                  ArtifactRow.revision == rev)).first()
        if row is None:
            return {"resolved": False}
        d = repo.latest_decision(tenant_id, aid, rev)
        return {"artifact_type": row.artifact_type, "release_stage": row.release_stage, "synthetic": row.synthetic,
                "source_sha256": row.source_sha256, "origin_node": row.origin_node,
                "decision": d.decision if d else None}
    row = {"source": lambda: s.get(SourceRow, (tenant_id, ref)), "blob": lambda: s.get(BlobRow, ref),
           "decision": lambda: s.get(DecisionRow, ref), "model_run": lambda: s.get(ModelRunRow, ref),
           "approval": lambda: s.get(ApprovalRow, ref), "release": lambda: s.get(ReleaseRow, ref),
           "work_package": lambda: s.get(WorkPackageRow, (tenant_id, ref)),
           "vendor_result": lambda: s.get(VendorResultRow, ref)}.get(kind, lambda: None)()
    if row is None or getattr(row, "tenant_id", tenant_id) != tenant_id:
        return {} if kind == "node" else {"resolved": False}
    if kind == "source":
        return {"approval_status": row.approval_status, "license": row.record.get("license"),
                "data_origin": row.record.get("data_origin")}
    if kind == "blob":
        return {"kind": row.kind, "filename": row.filename, "sha256": row.sha256, "status": row.status,
                "mime": row.sniffed_mime}
    if kind == "decision":
        return {"decision": row.decision, "validator_version": row.validator_version, "seq": row.seq}
    if kind == "model_run":
        r = row.record
        return {"model_name": r["model_name"], "model_version": r["model_version"], "provider": r["provider"],
                "prompt_revision": r["prompt_revision"], "status": r["status"],
                "estimated_cost_usd": r["estimated_cost_usd"]}
    if kind == "approval":
        return {"scope": row.scope, "decision": row.decision, "approver_id": row.approver_id}
    if kind == "release":
        return {"status": row.status, "target_nodes": row.record.get("target_nodes", []),
                "manifest_sha256": row.manifest_sha256}
    if kind == "work_package":
        return {"vendor_id": row.vendor_id, "status": row.status}
    if kind == "vendor_result":
        return {"status": row.status}
    return {}


def build_lineage(repo: Repository, tenant_id: str, artifact_id: str, revision: Optional[str] = None,
                  depth: int = 8, max_nodes: int = 500) -> Optional[LineageGraph]:
    revs = [r for r in repo.revisions(tenant_id, artifact_id) if revision is None or r.revision == revision]
    if not revs:
        return None
    start = {f"artifact:{artifact_id}@{r.revision}" for r in revs}
    seen, edges = set(start), {}
    for direction in ("up", "down"):
        frontier = set(start)
        for _ in range(depth):
            nxt = set()
            for e in repo.edges_touching(tenant_id, frontier):
                if direction == "up" and e.target in frontier:
                    edges[(e.source, e.target, e.relation)] = e
                    nxt.add(e.source)
                elif direction == "down" and e.source in frontier:
                    edges[(e.source, e.target, e.relation)] = e
                    nxt.add(e.target)
            nxt -= seen
            seen |= nxt
            frontier = nxt
            if not frontier or len(seen) >= max_nodes:
                break
    nodes = [LineageNode(id=n, kind=_kind(n), attrs=_attrs(repo, tenant_id, n)) for n in sorted(seen)]
    return LineageGraph(artifact_id=artifact_id, tenant_id=tenant_id, nodes=nodes,
                        edges=[LineageEdge(source=s, target=t, relation=r) for (s, t, r) in sorted(edges)])
