"""Artifact registry: the DAG the gateway resolves lineage, tenancy and evidence against.

Only RELEASED artifact revisions are registered (non-released submissions live in the
gateway's decision log). A revision is immutable: re-registering ``id@rev`` with a different
hash raises. Root artifacts cite their project's G0 rights grant, so every artifact has
upstream lineage (DQ06). Thread-safe; optionally persisted to a JSON file.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .manifest import APPROVED_RIGHTS, LicenseRights, OriginType, sha256_bytes

ArtifactStatus = Literal["ACTIVE", "SUPERSEDED", "REWORK"]
EvidenceKind = Literal["NDA", "CONSENT", "CALIBRATION", "CALIBRATION_REPORT", "SIGNED_TEST", "ECO", "WAIVER",
                       "SOURCE_DOC", "OTHER"]
GATES = ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")


class RegistryError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectRecord(BaseModel):
    project_id: str
    tenant_id: str
    product_ids: list[str] = []


class EvidenceRecord(BaseModel):
    evidence_id: str
    tenant_id: str
    project_id: str
    kind: EvidenceKind
    signed: bool = False
    signed_by: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    sha256: Optional[str] = None
    attributes: dict = {}


class ArtifactRecord(BaseModel):
    artifact_id: str
    revision: str
    tenant_id: str
    project_id: str
    node_id: str
    schema_key: str
    origin_type: OriginType
    rights: LicenseRights
    output_sha256: str
    sources: list[str] = []
    event_id: Optional[str] = None
    approved_by: Optional[str] = None
    approval_gate: Optional[str] = None
    approved_at: Optional[datetime] = None
    status: ArtifactStatus = "ACTIVE"
    status_reason: str = ""
    facts: dict = {}
    registered_at: datetime = Field(default_factory=utcnow)

    @property
    def ref(self) -> str:
        return f"{self.artifact_id}@{self.revision}"


class ApproverRecord(BaseModel):
    reviewer_id: str
    tenant_id: str
    gates: list[str]


class _State(BaseModel):
    projects: dict[str, ProjectRecord] = {}
    evidence: dict[str, EvidenceRecord] = {}
    artifacts: dict[str, ArtifactRecord] = {}          # key: "id@rev"
    order: dict[str, list[str]] = {}                   # artifact_id -> revisions in release order
    approvers: dict[str, ApproverRecord] = {}


class ArtifactRegistry:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        self._s = _State()
        if self._path and self._path.exists():
            self._s = _State.model_validate_json(self._path.read_text())

    # -- persistence ---------------------------------------------------------------------------
    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(self._s.model_dump_json(indent=1))
        os.replace(tmp, self._path)

    # -- projects, rights, approvers, evidence -------------------------------------------------------
    def register_project(self, project_id: str, tenant_id: str, product_ids: Optional[list[str]] = None) -> ProjectRecord:
        with self._lock:
            existing = self._s.projects.get(project_id)
            if existing and existing.tenant_id != tenant_id:
                raise RegistryError(f"project {project_id} belongs to another tenant")
            rec = ProjectRecord(project_id=project_id, tenant_id=tenant_id,
                                product_ids=sorted(set((existing.product_ids if existing else []) + (product_ids or []))))
            self._s.projects[project_id] = rec
            self._save()
            return rec

    def project(self, project_id: str) -> Optional[ProjectRecord]:
        return self._s.projects.get(project_id)

    def add_evidence(self, rec: EvidenceRecord) -> EvidenceRecord:
        with self._lock:
            if rec.evidence_id in self._s.evidence and self._s.evidence[rec.evidence_id] != rec:
                raise RegistryError(f"evidence {rec.evidence_id} already registered with different content")
            self._s.evidence[rec.evidence_id] = rec
            self._save()
            return rec

    def evidence(self, evidence_id: str) -> Optional[EvidenceRecord]:
        return self._s.evidence.get(evidence_id)

    def grant_rights(self, project_id: str, tenant_id: str, rights: LicenseRights, nda_evidence_id: str,
                     approved_by: str) -> ArtifactRecord:
        """G0: record contract/data rights for a project. Root artifacts cite ``G0-<project>@<n>``."""
        with self._lock:
            proj = self.project(project_id)
            if proj is None or proj.tenant_id != tenant_id:
                raise RegistryError(f"project {project_id} not registered for tenant {tenant_id}")
            nda = self.evidence(nda_evidence_id)
            if nda is None or nda.kind not in ("NDA", "CONSENT") or not nda.signed or nda.tenant_id != tenant_id:
                raise RegistryError("G0 needs a signed NDA/consent evidence record for this tenant")
            if rights not in APPROVED_RIGHTS:
                raise RegistryError("a G0 grant records approved rights only; unapproved projects get no grant")
            artifact_id = f"G0-{project_id}"
            rev = str(len(self._s.order.get(artifact_id, [])) + 1)
            body = json.dumps({"project": project_id, "rights": rights, "nda": nda_evidence_id, "rev": rev}).encode()
            rec = ArtifactRecord(artifact_id=artifact_id, revision=rev, tenant_id=tenant_id, project_id=project_id,
                                 node_id="X", schema_key="rights_grant.v1", origin_type="REAL", rights=rights,
                                 output_sha256=sha256_bytes(body), approved_by=approved_by, approval_gate="G0",
                                 facts={"nda_evidence_id": nda_evidence_id})
            self._put(rec)
            return rec

    def rights_grant(self, project_id: str) -> Optional[ArtifactRecord]:
        return self.current(f"G0-{project_id}")

    def add_approver(self, reviewer_id: str, tenant_id: str, gates: list[str]) -> ApproverRecord:
        bad = set(gates) - set(GATES)
        if bad:
            raise RegistryError(f"unknown gates {sorted(bad)}")
        with self._lock:
            rec = ApproverRecord(reviewer_id=reviewer_id, tenant_id=tenant_id, gates=sorted(set(gates)))
            self._s.approvers[reviewer_id] = rec
            self._save()
            return rec

    def approver(self, reviewer_id: str) -> Optional[ApproverRecord]:
        return self._s.approvers.get(reviewer_id)

    # -- artifacts -------------------------------------------------------------------------------------
    def _put(self, rec: ArtifactRecord) -> ArtifactRecord:
        owner = next(iter(self.revisions(rec.artifact_id)), None)
        if owner is not None and (owner.tenant_id != rec.tenant_id or owner.project_id != rec.project_id):
            raise RegistryError(f"artifact_id {rec.artifact_id} belongs to another project")
        existing = self._s.artifacts.get(rec.ref)
        if existing is not None:
            if existing.output_sha256 != rec.output_sha256:
                raise RegistryError(f"{rec.ref} is immutable and already registered with a different hash")
            return existing
        self._s.artifacts[rec.ref] = rec
        self._s.order.setdefault(rec.artifact_id, []).append(rec.revision)
        self._save()
        return rec

    def register(self, rec: ArtifactRecord) -> ArtifactRecord:
        with self._lock:
            return self._put(rec)

    def get(self, ref: str) -> Optional[ArtifactRecord]:
        return self._s.artifacts.get(ref)

    def current(self, artifact_id: str) -> Optional[ArtifactRecord]:
        revs = self._s.order.get(artifact_id)
        return self._s.artifacts.get(f"{artifact_id}@{revs[-1]}") if revs else None

    def revisions(self, artifact_id: str) -> list[ArtifactRecord]:
        return [self._s.artifacts[f"{artifact_id}@{r}"] for r in self._s.order.get(artifact_id, [])]

    def set_status(self, ref: str, status: ArtifactStatus, reason: str) -> None:
        with self._lock:
            rec = self._s.artifacts[ref]
            self._s.artifacts[ref] = rec.model_copy(update={"status": status, "status_reason": reason})
            self._save()

    def consumers_of(self, artifact_id: str) -> list[ArtifactRecord]:
        """Registered artifacts whose sources pin any revision of ``artifact_id``."""
        prefix = f"{artifact_id}@"
        return [a for a in self._s.artifacts.values() if any(s.startswith(prefix) for s in a.sources)]

    def artifacts_for_project(self, project_id: str, tenant_id: str) -> list[ArtifactRecord]:
        return [a for a in self._s.artifacts.values() if a.project_id == project_id and a.tenant_id == tenant_id]

    def all_artifacts(self) -> list[ArtifactRecord]:
        return list(self._s.artifacts.values())
