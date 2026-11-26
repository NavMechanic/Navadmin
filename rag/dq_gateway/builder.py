"""Manifest construction helper for producers, tests and demos.

It derives only what can be computed from real inputs: hashes from the actual bytes and the
lineage hash from the cited sources' registered hashes. Governance fields (rights,
classification, origin) have no defaults and must be stated by the caller.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .manifest import LicenseRights, DataClassification, OriginType, lineage_hash, sha256_bytes
from .registry import ArtifactRegistry


def build_manifest(*, registry: ArtifactRegistry, project_id: str, node_id: str, artifact_id: str, revision: str,
                   output_schema: str, content: bytes, sources: list[str], evidence_ids: list[str],
                   rights: LicenseRights, classification: DataClassification, origin: OriginType,
                   model_or_tool: str, connector_mode: str = "API", input_schema: str = "work_package.v1",
                   work_package_id: str = "WP-001", vendor_id: Optional[str] = None,
                   captured_at: Optional[datetime] = None, processed_at: Optional[datetime] = None,
                   measurement_units: Optional[dict[str, str]] = None, risk_method: str = "HEURISTIC",
                   risk_score: float = 0.1, candidate_actions: Optional[list[dict]] = None,
                   expected_cost_inr: Optional[str] = None, hard_policy_results: Optional[dict] = None,
                   source_revision: Optional[str] = None, event_id: Optional[str] = None,
                   **extra: Any) -> dict:
    resolved = [registry.get(s) for s in sources]
    hashes = [r.output_sha256 for r in resolved if r is not None]
    non_root = [r for r in resolved if r is not None and r.schema_key != "rights_grant.v1"]
    primary = non_root[0] if non_root else (resolved[0] if resolved and resolved[0] else None)
    now = datetime.now(timezone.utc)
    captured = captured_at or now
    manifest = {
        "event_id": event_id or str(uuid.uuid4()), "project_id": project_id, "work_package_id": work_package_id,
        "node_id": node_id, "vendor_id": vendor_id, "model_or_tool": model_or_tool, "connector_mode": connector_mode,
        "input_schema": input_schema, "output_schema": output_schema, "source_artifact_ids": sources,
        "source_revision": source_revision or (primary.revision if primary else "1"),
        "source_hash": lineage_hash(hashes) if len(hashes) == len(sources) and hashes else "0" * 64,
        "output_artifact_uri": f"artifact://{project_id}/{artifact_id}@{revision}",
        "output_file_sha256": sha256_bytes(content), "evidence_ids": evidence_ids,
        "source_license_rights": rights, "data_classification": classification, "origin_type": origin,
        "retention_and_deletion": {"retention_days": 730, "deletion": "on contract end"},
        "captured_at": captured.isoformat(), "processed_at": (processed_at or max(now, captured)).isoformat(),
        "measurement_units": measurement_units or {}, "uncertainty": None, "risk_method": risk_method,
        "risk_score": risk_score, "hard_policy_results": hard_policy_results or {},
        "candidate_actions": candidate_actions or [], "expected_cost_inr": expected_cost_inr,
    }
    manifest.update(extra)
    return manifest
