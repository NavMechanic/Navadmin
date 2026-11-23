"""Test support for the data pipeline: mock HTTP, registry mutation and artifact staging helpers."""
from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Union

import httpx

from data_pipeline.fetchers import DropZoneIntake, HostRateLimiter
from data_pipeline.policy_gates import (DOMAIN_BOUNDARY_SOURCES, DOMAIN_TRANSFER_WARNING, commercial_training_allowed,
                                        partition_for, sanitize_tag)
from data_pipeline.provenance import (MANIFEST_NAME, ProvenanceManifest, payload_digest, record_hash, sha256_bytes,
                                      write_manifest_once, write_record_index)
from data_pipeline.registry import Registry

REGISTRY = Registry.load()
NO_WAIT = HostRateLimiter(sleep=lambda s: None)          # tests never sleep for rate limits
NOW = datetime.now(timezone.utc)
_METHOD = {"git": "git_clone", "http_archive": "http_archive", "api": "api_snapshot", "drop_zone": "manual_drop_zone",
           "vault": "vault_intake"}
Handler = Union[httpx.Response, Callable[[httpx.Request], httpx.Response]]


class MockWeb:
    """``httpx.MockTransport`` with prefix routes and a request log."""

    def __init__(self, routes: Optional[dict[str, Handler]] = None, default: Optional[Handler] = None) -> None:
        self.routes, self.default, self.calls = dict(routes or {}), default, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                h = self.routes[prefix]
                return h(request) if callable(h) else h
        if self.default is not None:
            return self.default(request) if callable(self.default) else self.default
        return httpx.Response(404, text="not found")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True)


def license_page(text: str = "Permission is hereby granted under the license terms. " * 3) -> httpx.Response:
    return httpx.Response(200, text=f"<html><body><h1>License</h1><p>{text}</p></body></html>",
                          headers={"content-type": "text/html"})


def raw_registry() -> dict:
    return copy.deepcopy(Registry.raw())


def mutated_registry(source_id: str, **changes) -> dict:
    data = raw_registry()
    row = next(r for r in data["sources"] if r["source_id"] == source_id)
    for k, v in changes.items():
        if v is None:
            row.pop(k, None)
        else:
            row[k] = v
    return data


def stage(lake, source_id: str, files: dict[str, bytes], *, partition: Optional[str] = None, node: Optional[str] = None,
          subdir: str = "", **overrides) -> Path:
    """Write an artifact + valid write-once manifest exactly as a fetcher would (optionally misplaced)."""
    spec = REGISTRY.get(source_id)
    parent = lake.root / (partition or partition_for(spec)) / (node or spec.node_id) / source_id
    if subdir:
        parent = parent / subdir
    tmp = parent / f".tmp-{uuid.uuid4().hex[:8]}"
    (tmp / "artifact").mkdir(parents=True)
    for name, data in files.items():
        p = tmp / "artifact" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    sha, entries = payload_digest(tmp / "artifact")
    live = spec.intended_use == "LIVE-LOOKUP"
    fields = dict(
        source_id=source_id, node_id=spec.node_id, intended_use=spec.intended_use, official_url=spec.official_url,
        fetched_at_utc=NOW - timedelta(minutes=5), license_snapshot_text=spec.license_text or f"{spec.license_spdx_id} terms",
        license_spdx_id=spec.license_spdx_id, sha256=sha, version_tag=spec.version_tag, origin_type=spec.origin_type,
        data_steward=spec.data_steward, partition=partition_for(spec),
        fetch_method="api_live_lookup" if live else _METHOD[spec.access], artifact_files=entries,
        commercial_training_allowed=commercial_training_allowed(spec), training_allowed=spec.training_allowed,
        domain_transfer_warning=DOMAIN_TRANSFER_WARNING if source_id in DOMAIN_BOUNDARY_SOURCES else None,
        transport=None if spec.access in ("vault", "drop_zone") else {"tls_verified": True},
        entitlement={"entitlement_id": "PO-1"} if spec.access == "drop_zone" else None)
    fields.update(overrides)
    m = ProvenanceManifest(**fields)
    write_manifest_once(tmp, m)
    final = parent / f"{sanitize_tag(m.version_tag)}__{sha[:12]}"
    os.replace(tmp, final)
    return final


def edit_manifest(artifact_dir: Path, **changes) -> None:
    """Bypass the write-once writer (simulates on-disk tampering / a forged manifest)."""
    path = Path(artifact_dir) / MANIFEST_NAME
    data = json.loads(path.read_text())
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    os.chmod(path, 0o644)
    path.write_text(json.dumps(data, indent=2, default=str))


def add_formatted(artifact_dir: Path, records: list[dict], name: str = "slm.jsonl",
                  hashes: Optional[set[str]] = None, splits: Optional[dict] = None) -> set[str]:
    out = Path(artifact_dir) / "formatted"
    out.mkdir(exist_ok=True)
    with open(out / name, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    hs = hashes if hashes is not None else {record_hash(r) for r in records}
    write_record_index(out, hs)
    if splits is not None:
        (out / "splits.json").write_text(json.dumps(splits))
    (out / "format_manifest.json").write_text(json.dumps({"builder": "test"}))
    return hs


def approve(lake, source_id: str, sha: str, decision: str = "APPROVED") -> dict:
    return lake.ledger.record(source_id=source_id, sha256=sha, decision=decision,
                              steward_id=REGISTRY.get(source_id).data_steward, registry=REGISTRY, rationale="test review")


def manifest_sha(artifact_dir: Path) -> str:
    return json.loads((Path(artifact_dir) / MANIFEST_NAME).read_text())["sha256"]


def drop(lake, sid: str, name: str, data: bytes, write_entitlement: bool = True, **ent_changes) -> Path:
    source_id, spec = sid, REGISTRY.get(sid)
    zone = DropZoneIntake(spec, lake, REGISTRY).ensure_zone()
    path = zone / "inbox" / name
    path.write_bytes(data)
    if write_entitlement:
        ent = {"source_id": source_id, "file_name": name, "sha256": sha256_bytes(data), "entitlement_id": "PO-2026-0042",
               "licensee": "ATHENA Pvt Ltd", "license_type": "purchased-single-user", "permitted_uses": ["RAG/REFERENCE",
                                                                                                         "EVAL/BENCHMARK"],
               "valid_from": (NOW - timedelta(days=30)).isoformat(), "valid_until": (NOW + timedelta(days=300)).isoformat(),
               "acquired_via": "purchase", "acquired_by": "engineer.design", "steward_id": spec.data_steward,
               "license_snapshot_text": "Single-user licence for internal reference use only; no redistribution, no AI training."}
        ent.update(ent_changes)
        (zone / "inbox" / f"{name}.entitlement.json").write_text(json.dumps(ent))
    return path
