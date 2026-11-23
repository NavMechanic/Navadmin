"""Canonical I-O Manifest — the 33-field envelope every artifact must carry.

Conventions (all enforced by validators, so a malformed envelope fails DQ02):
  * ``output_artifact_uri`` is ``artifact://<project_id>/<artifact_id>@<revision>`` — it is the
    artifact's identity and immutable revision.
  * ``source_artifact_ids`` are revision-pinned refs ``<artifact_id>@<revision>``; root artifacts
    cite their G0 rights grant (``G0-<project_id>@1``) so lineage is never empty.
  * ``model_or_tool`` is ``<kind>:<name>[@<version>]`` with kind in
    llm|slm|ml|tool|engine|solver|sensor|human|vendor — rules DQ12/DQ23/DQ26 key off the kind.
  * schemas are ``<name>.v<major>`` or ``<name>@<major>.<minor>.<patch>``.
  * timestamps are timezone-aware UTC.

The model is frozen: the gateway never edits a submitted manifest, it emits an evaluated copy.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NodeId = Literal["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "X"]
ConnectorMode = Literal["API", "SDK", "MCP", "CLI", "BATCH", "PORTAL", "MANUAL"]
LicenseRights = Literal["APPROVED", "APPROVED_NO_TRAIN", "UNVERIFIED", "PROHIBITED"]
DataClassification = Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
OriginType = Literal["REAL", "SYNTHETIC", "DERIVED"]
RiskMethod = Literal["HEURISTIC", "CALIBRATED_ML", "SOLVER"]
PolicyResult = Literal["PASS", "FAIL", "UNKNOWN"]
QualityGate = Literal["BLOCK", "QUARANTINE", "REWORK", "READY", "HOLD", "RELEASED"]

APPROVED_RIGHTS = frozenset({"APPROVED", "APPROVED_NO_TRAIN"})
SHA256 = r"^[0-9a-f]{64}$"
_SCHEMA = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)(?:\.v(?P<major>\d+)|@(?P<semver>(?P<smajor>\d+)\.\d+\.\d+))$")
_URI = re.compile(r"^artifact://(?P<project>[A-Za-z0-9._-]+)/(?P<artifact>[A-Za-z0-9._:-]+)@(?P<rev>[A-Za-z0-9._-]+)$")
_REF = re.compile(r"^(?P<artifact>[A-Za-z0-9._:-]+)@(?P<rev>[A-Za-z0-9._-]+)$")
_TOOL = re.compile(r"^(?P<kind>llm|slm|ml|tool|engine|solver|sensor|human|vendor):(?P<name>[A-Za-z0-9._/-]+)"
                   r"(?:@(?P<version>[A-Za-z0-9._-]+))?$")
AI_KINDS = frozenset({"llm", "slm", "ml"})
LANGUAGE_MODEL_KINDS = frozenset({"llm", "slm"})


def schema_key(schema_id: str) -> str:
    """Normalize ``name@1.2.3`` / ``name.v1`` to the registry key ``name.v1``."""
    m = _SCHEMA.match(schema_id or "")
    if not m:
        raise ValueError(f"schema id {schema_id!r} must be '<name>.v<major>' or '<name>@<major>.<minor>.<patch>'")
    return f"{m.group('name')}.v{m.group('major') or m.group('smajor')}"


def parse_ref(ref: str) -> tuple[str, str]:
    m = _REF.match(ref or "")
    if not m:
        raise ValueError(f"source ref {ref!r} must be '<artifact_id>@<revision>'")
    return m.group("artifact"), m.group("rev")


def parse_artifact_uri(uri: str) -> tuple[str, str, str]:
    m = _URI.match(uri or "")
    if not m:
        raise ValueError(f"output_artifact_uri {uri!r} must be 'artifact://<project>/<artifact_id>@<revision>'")
    return m.group("project"), m.group("artifact"), m.group("rev")


def tool_kind(model_or_tool: str) -> str:
    m = _TOOL.match(model_or_tool or "")
    if not m:
        raise ValueError(f"model_or_tool {model_or_tool!r} must be '<kind>:<name>[@<version>]'")
    return m.group("kind")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree_bytes(path: str | Path, extensions: Optional[set[str]] = None) -> bytes:
    """Deterministic byte representation of a file or directory (sorted relpath + file hash lines).

    The SHA-256 of this is the declared ``output_file_sha256`` for a directory bundle.
    """
    p = Path(path)
    if p.is_file():
        return p.read_bytes()
    files = sorted((f.relative_to(p).as_posix(), f) for f in p.rglob("*")
                   if f.is_file() and (not extensions or f.suffix.lower() in extensions))
    return "".join(f"{rel}\t{sha256_file(f)}\n" for rel, f in files).encode()


def lineage_hash(source_hashes: list[str]) -> str:
    """``source_hash`` for multi-source artifacts: SHA-256 over the sorted upstream output hashes."""
    return sha256_bytes("\n".join(sorted(source_hashes)).encode())


def _require_utc(v: datetime, name: str) -> datetime:
    if v.tzinfo is None or v.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC (e.g. 2026-11-23T10:00:00Z)")
    return v


class CanonicalIOManifest(BaseModel):
    """The 33-field Canonical I-O Manifest."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    # identity & routing (9)
    event_id: UUID
    project_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    work_package_id: str = Field(min_length=1, max_length=100)
    node_id: NodeId
    vendor_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    model_or_tool: str
    connector_mode: ConnectorMode
    input_schema: str
    output_schema: str
    # lineage & integrity (6)
    source_artifact_ids: list[str]
    source_revision: str = Field(min_length=1, max_length=50)
    source_hash: str = Field(pattern=SHA256)
    output_artifact_uri: str
    output_file_sha256: str = Field(pattern=SHA256)
    evidence_ids: list[str]
    # governance & classification (4)
    source_license_rights: LicenseRights
    data_classification: DataClassification
    origin_type: OriginType
    retention_and_deletion: dict[str, Any]
    # timing & units (3)
    captured_at: datetime
    processed_at: datetime
    measurement_units: dict[str, str]
    # risk & policy (6)
    uncertainty: dict[str, Any] | float | None = None
    risk_method: RiskMethod
    risk_score: float = Field(ge=0.0, le=1.0)
    hard_policy_results: dict[str, PolicyResult]
    candidate_actions: list[dict[str, Any]]
    expected_cost_inr: Optional[Decimal] = Field(default=None, ge=0)
    # gate & approval (5)
    quality_gate: QualityGate = "READY"
    reviewer_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    approval_timestamp: Optional[datetime] = None
    actual_outcome: Optional[dict[str, Any]] = None
    feedback_authorized: bool = False

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema(cls, v: str) -> str:
        schema_key(v)
        return v

    @field_validator("model_or_tool")
    @classmethod
    def _tool(cls, v: str) -> str:
        tool_kind(v)
        return v

    @field_validator("source_artifact_ids")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        for ref in v:
            parse_ref(ref)
        if len(set(v)) != len(v):
            raise ValueError("source_artifact_ids must be unique")
        return v

    @field_validator("output_artifact_uri")
    @classmethod
    def _uri(cls, v: str) -> str:
        parse_artifact_uri(v)
        return v

    @field_validator("captured_at", "processed_at", "approval_timestamp")
    @classmethod
    def _utc(cls, v: Optional[datetime], info) -> Optional[datetime]:
        return None if v is None else _require_utc(v, info.field_name)

    @field_validator("retention_and_deletion")
    @classmethod
    def _retention(cls, v: dict) -> dict:
        days = v.get("retention_days")
        if not isinstance(days, int) or isinstance(days, bool) or days < 0:
            raise ValueError("retention_and_deletion.retention_days must be a non-negative integer")
        return v

    @model_validator(mode="after")
    def _cross_field(self) -> "CanonicalIOManifest":
        if self.processed_at < self.captured_at:
            raise ValueError("processed_at must be >= captured_at")
        project, _, _ = parse_artifact_uri(self.output_artifact_uri)
        if project != self.project_id:
            raise ValueError("output_artifact_uri project must equal project_id")
        if tool_kind(self.model_or_tool) == "vendor" and not self.vendor_id:
            raise ValueError("vendor_id is required for vendor-sourced artifacts")
        if (self.reviewer_id is None) != (self.approval_timestamp is None):
            raise ValueError("reviewer_id and approval_timestamp must be set together")
        if self.approval_timestamp is not None and self.approval_timestamp < self.processed_at:
            raise ValueError("approval_timestamp must be >= processed_at")
        return self

    # convenience ----------------------------------------------------------------
    @property
    def artifact_id(self) -> str:
        return parse_artifact_uri(self.output_artifact_uri)[1]

    @property
    def revision(self) -> str:
        return parse_artifact_uri(self.output_artifact_uri)[2]

    @property
    def ref(self) -> str:
        return f"{self.artifact_id}@{self.revision}"

    @property
    def tool_kind(self) -> str:
        return tool_kind(self.model_or_tool)

    @property
    def output_schema_key(self) -> str:
        return schema_key(self.output_schema)


MANIFEST_FIELDS = tuple(CanonicalIOManifest.model_fields)
assert len(MANIFEST_FIELDS) == 33, len(MANIFEST_FIELDS)
