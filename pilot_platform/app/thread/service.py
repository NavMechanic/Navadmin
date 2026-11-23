"""Digital thread: immutable, content-hashed artifact revisions with lineage.

* Kinds follow the workflow (RFQ, PRD, BOM, SIM, TEST, QUOTE, BUILD) plus MARKET for N1 output.
* A revision never changes. Writing new content appends rev N+1, linked to rev N.
* Non-root kinds must cite at least one parent revision (derived_from), and parents
  must belong to the same tenant — lineage cannot cross tenants.
* Writing identical content with identical parents is idempotent (returns the existing rev).
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.audit import canonical_json, jsonable
from ..core.errors import NotFound, ValidationFailed
from ..models import Artifact, ArtifactRevision, RevisionLink

KINDS = ("RFQ", "PRD", "BOM", "SIM", "TEST", "QUOTE", "BUILD", "MARKET")
ROOT_KINDS = frozenset({"RFQ", "MARKET"})


def content_hash(content: Any) -> str:
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


def _parents_of(db: Session, revision_id: int) -> list[int]:
    return sorted(db.scalars(select(RevisionLink.parent_id).where(RevisionLink.child_id == revision_id)).all())


def get_revision(db: Session, tenant_id: str, revision_id: int) -> ArtifactRevision:
    rev = db.get(ArtifactRevision, revision_id)
    if rev is None or rev.artifact.tenant_id != tenant_id:
        raise NotFound(f"revision {revision_id} not found")
    return rev


def create_revision(db: Session, tenant_id: str, actor: str, kind: str, key: str, content: dict,
                    derived_from: Iterable[int] = (), note: str = "") -> ArtifactRevision:
    if kind not in KINDS:
        raise ValidationFailed(f"unknown artifact kind {kind!r}; expected one of {KINDS}")
    if not key or len(key) > 200:
        raise ValidationFailed("artifact key must be 1-200 characters")
    content = jsonable(content)
    parent_ids = sorted(set(int(i) for i in derived_from))
    if parent_ids:
        found = db.scalars(select(ArtifactRevision.id).join(Artifact)
                           .where(ArtifactRevision.id.in_(parent_ids), Artifact.tenant_id == tenant_id)).all()
        missing = sorted(set(parent_ids) - set(found))
        if missing:
            raise ValidationFailed("unknown or cross-tenant parent revisions", revision_ids=missing)

    artifact = db.scalars(select(Artifact).where(Artifact.tenant_id == tenant_id, Artifact.kind == kind,
                                                 Artifact.key == key)).first()
    latest = None
    if artifact is None:
        if kind not in ROOT_KINDS and not parent_ids:
            raise ValidationFailed(f"{kind} revisions must be derived_from at least one upstream revision")
        artifact = Artifact(tenant_id=tenant_id, kind=kind, key=key)
        db.add(artifact)
        db.flush()
    else:
        latest = db.scalars(select(ArtifactRevision).where(ArtifactRevision.artifact_id == artifact.id)
                            .order_by(ArtifactRevision.rev.desc())).first()

    digest = content_hash(content)
    if latest is not None and latest.content_hash == digest:
        explicit = set(_parents_of(db, latest.id)) - {latest.id}
        if set(parent_ids) <= explicit or not parent_ids:
            return latest  # idempotent re-write

    rev = ArtifactRevision(artifact_id=artifact.id, rev=(latest.rev + 1) if latest else 1, content=content,
                           content_hash=digest, created_by=actor, note=note)
    db.add(rev)
    db.flush()
    links = set(parent_ids) | ({latest.id} if latest else set())
    for pid in sorted(links):
        db.add(RevisionLink(child_id=rev.id, parent_id=pid))
    db.flush()
    audit.record(db, tenant_id, actor, "artifact.revision_created", "artifact_revision", rev.id,
                 {"kind": kind, "key": key, "rev": rev.rev, "parents": sorted(links), "hash": digest})
    return rev


def latest_revision(db: Session, tenant_id: str, kind: str, key: str | None = None) -> ArtifactRevision | None:
    q = select(ArtifactRevision).join(Artifact).where(Artifact.tenant_id == tenant_id, Artifact.kind == kind)
    if key is not None:
        q = q.where(Artifact.key == key)
    return db.scalars(q.order_by(ArtifactRevision.id.desc())).first()


def revision_view(db: Session, rev: ArtifactRevision, include_content: bool = True) -> dict:
    out = {"id": rev.id, "kind": rev.artifact.kind, "key": rev.artifact.key, "rev": rev.rev,
           "content_hash": rev.content_hash, "created_by": rev.created_by, "note": rev.note,
           "created_at": rev.created_at.isoformat(), "parents": _parents_of(db, rev.id)}
    if include_content:
        out["content"] = rev.content
    return out


def lineage(db: Session, tenant_id: str, revision_id: int) -> dict:
    """Every ancestor of a revision (the upstream digital thread), nearest first."""
    root = get_revision(db, tenant_id, revision_id)
    seen: dict[int, dict] = {}
    frontier = [root.id]
    while frontier:
        nxt = []
        for rid in frontier:
            for pid in _parents_of(db, rid):
                if pid not in seen:
                    seen[pid] = revision_view(db, db.get(ArtifactRevision, pid), include_content=False)
                    nxt.append(pid)
        frontier = nxt
    kinds = sorted({v["kind"] for v in seen.values()} | {root.artifact.kind})
    return {"revision": revision_view(db, root, include_content=False), "ancestors": list(seen.values()), "kinds": kinds}


def descendants(db: Session, tenant_id: str, revision_id: int) -> list[dict]:
    get_revision(db, tenant_id, revision_id)
    seen: dict[int, dict] = {}
    frontier = [revision_id]
    while frontier:
        children = db.scalars(select(RevisionLink.child_id).where(RevisionLink.parent_id.in_(frontier))).all()
        frontier = [c for c in children if c not in seen]
        for c in frontier:
            seen[c] = revision_view(db, db.get(ArtifactRevision, c), include_content=False)
    return list(seen.values())


def diff(old: Any, new: Any, path: str = "") -> list[dict]:
    """Structural JSON diff: list of {path, op: added|removed|changed, old, new}."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in sorted(set(old) | set(new), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in old:
                out.append({"path": p, "op": "added", "new": new[k]})
            elif k not in new:
                out.append({"path": p, "op": "removed", "old": old[k]})
            else:
                out.extend(diff(old[k], new[k], p))
        return out
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        out = []
        for i, (a, b) in enumerate(zip(old, new)):
            out.extend(diff(a, b, f"{path}[{i}]"))
        return out
    return [] if old == new else [{"path": path or "$", "op": "changed", "old": old, "new": new}]


def coverage(db: Session, tenant_id: str) -> dict:
    """Share of non-root revisions that are linked to an upstream revision (gate E)."""
    rows = db.execute(select(ArtifactRevision.id, Artifact.kind).join(Artifact)
                      .where(Artifact.tenant_id == tenant_id)).all()
    linked = set(db.scalars(select(RevisionLink.child_id).distinct()).all())
    needing = [rid for rid, kind in rows if kind not in ROOT_KINDS]
    unlinked = sorted(rid for rid in needing if rid not in linked)
    total = len(needing)
    counts = dict(db.execute(select(Artifact.kind, func.count(ArtifactRevision.id)).join(ArtifactRevision)
                             .where(Artifact.tenant_id == tenant_id).group_by(Artifact.kind)).all())
    return {"revisions": len(rows), "non_root_revisions": total, "unlinked_revision_ids": unlinked,
            "coverage": 1.0 if total == 0 else round((total - len(unlinked)) / total, 4), "by_kind": counts}
