"""Shared intake checks for the minimal N6 / N7 receivers of N5 sourcing handoffs.

A receiver accepts a SOURCING_HANDOFF only if it is the current revision, N0 RELEASED it to that node,
its stored payload bytes still match their SHA-256, it targets that node, and N5 reports the handoff
as live (not BLOCKED by a rights revocation or a BOM / PRD revision change).
"""
from __future__ import annotations

from typing import Any

from ..errors import N0Error
from ..repository import Repository
from ..rules import sha256_hex
from ..security import Principal


def verify_released_handoff(state, p: Principal, artifact_id: str, revision: str, node: str) -> tuple[dict, Any]:
    t = p.tenant_id
    with state.db.session() as s:
        repo = Repository(s)
        row = repo.get_artifact(t, artifact_id, revision)
        if row is None or row.artifact_type != "SOURCING_HANDOFF":
            raise N0Error(404, "NOT_A_SOURCING_HANDOFF", f"{artifact_id}@{revision} is not a SOURCING_HANDOFF")
        latest = repo.latest_revision(t, artifact_id)
        if latest is not None and latest.revision_order > row.revision_order:
            raise N0Error(409, "SUPERSEDED_REVISION", f"{artifact_id}@{revision} is superseded by @{latest.revision}")
        manifest = next((m for m in reversed(repo.releases(t, artifact_id, revision))
                         if m.status == "RELEASED" and node in m.target_nodes), None)
        if manifest is None:
            raise N0Error(409, "NOT_RELEASED_TO_NODE", f"N0 has not released {artifact_id}@{revision} to {node}")
        blob = repo.find_blob(t, row.payload_sha256, "PAYLOAD")
        if blob is None or sha256_hex(state.store.get(blob.bucket, blob.object_key)) != row.payload_sha256 or \
                manifest.payload_sha256 != row.payload_sha256:
            raise N0Error(409, "PAYLOAD_HASH_MISMATCH", f"stored payload of {artifact_id}@{revision} does not verify")
        payload = row.canonical["payload"]
        synthetic = row.synthetic
    if payload.get("target") != node:
        raise N0Error(409, "WRONG_TARGET", f"{artifact_id}@{revision} targets {payload.get('target')}, not {node}")
    h = state.n5.handoff_for_artifact(t, artifact_id, revision)
    if h is None:
        raise N0Error(409, "HANDOFF_UNKNOWN", f"N5 has no released handoff for {artifact_id}@{revision}")
    live = state.n5.handoff_status(t, h.handoff_id)
    if live["status"] == "BLOCKED":
        raise N0Error(409, "HANDOFF_BLOCKED", f"N5 handoff {h.handoff_id} is BLOCKED: {'; '.join(live['reasons'])}")
    return {**payload, "_synthetic": synthetic, "_manifest_id": manifest.manifest_id}, h
