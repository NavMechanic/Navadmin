"""Immutable provenance: SHA-256 hashing, license snapshots, the sidecar manifest, and
W3C PROV-O / SPDX 2.3 / CycloneDX 1.6 metadata.

Each staged artifact directory looks like::

    <version>__<sha12>/
      artifact/                    the fetched bytes (single file or tree)
      provenance_manifest.json     write-once sidecar (this module's ProvenanceManifest)
      prov.jsonld                  W3C PROV-O lineage
      spdx.json                    SPDX 2.3 document (package + file checksums + license)
      cyclonedx.json               CycloneDX 1.6 data-component BOM
      formatted/                   builder outputs (+ record_index.json), same partition

``sha256`` is the SHA-256 of the file when the artifact is a single file, otherwise the SHA-256 of
the sorted ``relpath<TAB>sha256`` listing of every file (a tree digest).
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .policy_gates import PolicyViolation

MANIFEST_NAME = "provenance_manifest.json"
MANIFEST_SCHEMA = "athena.provenance_manifest.v1"
TOOL = "athena-data-pipeline/1.0"
REQUIRED_FIELDS = ("source_id", "node_id", "intended_use", "official_url", "fetched_at_utc", "license_snapshot_text",
                   "license_spdx_id", "sha256", "version_tag", "origin_type", "training_rights_approval", "data_steward")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(Exception):
    pass


class FileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str
    bytes: int
    mime: Optional[str] = None


class ProvenanceManifest(BaseModel):
    """The sidecar. The 12 required fields come first; the rest are declared extras (extra='forbid')."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^SRC-\d{3}$")
    node_id: str = Field(pattern=r"^N[0-8]$")
    intended_use: Literal["TRAIN-CANDIDATE", "RAG/REFERENCE", "LIVE-LOOKUP", "EVAL/BENCHMARK", "PRIVATE-GROUND-TRUTH"]
    official_url: str
    fetched_at_utc: datetime
    license_snapshot_text: str = Field(min_length=1)
    license_spdx_id: str = Field(min_length=1)
    sha256: str
    version_tag: str = Field(min_length=1)
    origin_type: Literal["PUBLIC_OPEN", "LIVE_API", "PRIVATE_GROUND_TRUTH", "SYNTHETIC_TEST_CASE"]
    training_rights_approval: Literal["NOT_REVIEWED", "PENDING", "APPROVED", "REJECTED"] = "NOT_REVIEWED"
    data_steward: str = Field(pattern=r"^steward\.[a-z0-9.\-]+$")

    manifest_schema: Literal["athena.provenance_manifest.v1"] = MANIFEST_SCHEMA
    source_name: Optional[str] = None
    partition: Optional[str] = None
    fetch_method: Optional[Literal["git_clone", "git_tarball", "http_archive", "api_snapshot", "api_live_lookup",
                                   "manual_drop_zone", "vault_intake", "vault_stub", "synthetic_fixture"]] = None
    artifact_files: list[FileEntry] = []
    license_snapshot_sha256: Optional[str] = None
    license_source: Optional[str] = None
    commercial_training_allowed: bool = False
    training_allowed: bool = False
    domain_transfer_warning: Optional[str] = None
    catalog_status: Optional[str] = None
    entitlement: Optional[dict[str, Any]] = None
    transport: Optional[dict[str, Any]] = None
    cache_expires_at_utc: Optional[datetime] = None
    query_key: Optional[str] = None
    redactions: int = 0
    derived_from: list[str] = []
    tool: str = TOOL

    @field_validator("sha256", "license_snapshot_sha256")
    @classmethod
    def _hex(cls, v):
        if v is not None and not _HEX64.match(v):
            raise ValueError("must be a lowercase 64-hex SHA-256")
        return v

    @field_validator("fetched_at_utc", "cache_expires_at_utc")
    @classmethod
    def _utc(cls, v):
        if v is None:
            return v
        if v.tzinfo is None or v.utcoffset() != timedelta(0):
            raise ValueError("timestamps must be timezone-aware UTC")
        return v

    @field_validator("official_url")
    @classmethod
    def _url(cls, v):
        if not (v.startswith("https://") or v.startswith("internal://athena/")):
            raise ValueError("official_url must be https:// or internal://athena/")
        return v

    @model_validator(mode="after")
    def _checks(self):
        if self.fetched_at_utc > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("fetched_at_utc is in the future")
        if self.license_snapshot_sha256 and self.license_snapshot_sha256 != sha256_bytes(self.license_snapshot_text.encode()):
            raise ValueError("license_snapshot_sha256 does not match license_snapshot_text")
        return self


# ----------------------------------------------------------------------------------------------
# hashing
# ----------------------------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def payload_digest(root: Path, mime_of=None) -> tuple[str, list[FileEntry]]:
    """(sha256, files) for ``root`` (a file or a directory tree)."""
    root = Path(root)
    if root.is_file():
        files = [FileEntry(path=root.name, sha256=sha256_file(root), bytes=root.stat().st_size,
                           mime=mime_of(root) if mime_of else None)]
        return files[0].sha256, files
    paths = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    files = [FileEntry(path=p.relative_to(root).as_posix(), sha256=sha256_file(p), bytes=p.stat().st_size,
                       mime=mime_of(p) if mime_of else None) for p in paths]
    if len(files) == 1:
        return files[0].sha256, files
    listing = "".join(f"{f.path}\t{f.sha256}\n" for f in files)
    return sha256_bytes(listing.encode()), files


def verify_payload(artifact_dir: Path, manifest: ProvenanceManifest) -> tuple[bool, str]:
    payload = Path(artifact_dir) / "artifact"
    if not payload.exists():
        return False, "artifact/ payload is missing"
    if any(p.is_symlink() for p in payload.rglob("*")):
        return False, "artifact payload contains symlinks"
    sha, files = payload_digest(payload)
    if sha != manifest.sha256:
        return False, f"payload SHA-256 {sha[:12]}… does not match manifest {manifest.sha256[:12]}… (tampered)"
    if manifest.artifact_files and {f.path: f.sha256 for f in files} != {f.path: f.sha256 for f in manifest.artifact_files}:
        return False, "payload file list differs from the manifest"
    return True, "SHA-256 verified"


# ----------------------------------------------------------------------------------------------
# license snapshots and secrets
# ----------------------------------------------------------------------------------------------
def html_to_text(raw: str, limit: int = 500_000) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = re.sub(r"[ \t\r\f\v]+", " ", html.unescape(raw))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit]


def redact(data: bytes, secrets: Iterable[str]) -> tuple[bytes, int]:
    n = 0
    for s in secrets:
        if s and len(s) >= 6 and s.encode() in data:
            n += data.count(s.encode())
            data = data.replace(s.encode(), b"[REDACTED]")
    return data, n


def assert_no_secrets(text: str, secrets: Iterable[str], where: str) -> None:
    for s in secrets:
        if s and len(s) >= 6 and s in text:
            raise PolicyViolation("HC-06", f"credential value would be written into {where}")


# ----------------------------------------------------------------------------------------------
# manifest I/O (write-once)
# ----------------------------------------------------------------------------------------------
def load_manifest(artifact_dir: Path) -> ProvenanceManifest:
    path = Path(artifact_dir) / MANIFEST_NAME
    if not path.exists():
        raise ManifestError(f"{path} is missing")
    try:
        return ProvenanceManifest.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        raise ManifestError(f"invalid provenance manifest {path}: {exc.errors(include_url=False)[:3]}")


def write_manifest_once(artifact_dir: Path, manifest: ProvenanceManifest, secrets: Iterable[str] = ()) -> Path:
    body = manifest.model_dump_json(indent=2)
    assert_no_secrets(body, secrets, "provenance_manifest.json")
    path = Path(artifact_dir) / MANIFEST_NAME
    if path.exists():
        if path.read_text(encoding="utf-8") == body:
            return path
        raise PolicyViolation("HC-10", f"{path} is immutable; a different manifest cannot overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, 0o444)
    for name, doc in (("prov.jsonld", prov_o(manifest)), ("spdx.json", spdx_document(manifest)),
                      ("cyclonedx.json", cyclonedx_bom([manifest]))):
        side = Path(artifact_dir) / name
        if not side.exists():
            side.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return path


# ----------------------------------------------------------------------------------------------
# W3C PROV-O, SPDX 2.3, CycloneDX 1.6
# ----------------------------------------------------------------------------------------------
def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prov_o(m: ProvenanceManifest) -> dict:
    entity = f"urn:athena:artifact:{m.source_id}:{m.sha256}"
    activity = f"urn:athena:activity:{m.fetch_method or 'acquire'}:{m.source_id}:{_ts(m.fetched_at_utc)}"
    return {
        "@context": {"prov": "http://www.w3.org/ns/prov#", "xsd": "http://www.w3.org/2001/XMLSchema#",
                     "rdfs": "http://www.w3.org/2000/01/rdf-schema#", "athena": "urn:athena:vocab:"},
        "@graph": [
            {"@id": entity, "@type": "prov:Entity", "rdfs:label": f"{m.source_id} {m.version_tag}",
             "prov:wasDerivedFrom": {"@id": m.official_url}, "prov:wasGeneratedBy": {"@id": activity},
             "prov:generatedAtTime": {"@value": _ts(m.fetched_at_utc), "@type": "xsd:dateTime"},
             "athena:sha256": m.sha256, "athena:licenseSpdxId": m.license_spdx_id, "athena:intendedUse": m.intended_use,
             "athena:originType": m.origin_type, "athena:trainingRightsApproval": m.training_rights_approval,
             **({"athena:domainTransferWarning": m.domain_transfer_warning} if m.domain_transfer_warning else {}),
             **({"prov:wasDerivedFrom": [{"@id": m.official_url}] + [{"@id": f"urn:athena:sha256:{d}"} for d in m.derived_from]}
                if m.derived_from else {})},
            {"@id": m.official_url, "@type": "prov:Entity", "rdfs:label": m.source_name or m.source_id},
            {"@id": activity, "@type": "prov:Activity", "prov:used": {"@id": m.official_url},
             "prov:endedAtTime": {"@value": _ts(m.fetched_at_utc), "@type": "xsd:dateTime"},
             "prov:wasAssociatedWith": {"@id": "urn:athena:agent:athena-data-pipeline"}},
            {"@id": "urn:athena:agent:athena-data-pipeline", "@type": ["prov:Agent", "prov:SoftwareAgent"],
             "rdfs:label": m.tool, "prov:actedOnBehalfOf": {"@id": f"urn:athena:agent:{m.data_steward}"}},
            {"@id": f"urn:athena:agent:{m.data_steward}", "@type": "prov:Agent", "rdfs:label": m.data_steward},
        ],
    }


def spdx_document(m: ProvenanceManifest, max_files: int = 5000) -> dict:
    pkg = f"SPDXRef-Package-{m.source_id}"
    doc = {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{m.source_id}-{m.version_tag}",
        "documentNamespace": f"urn:athena:spdx:{m.source_id}:{m.sha256}",
        "creationInfo": {"created": _ts(m.fetched_at_utc), "creators": [f"Tool: {m.tool.replace('/', '-')}",
                                                                         f"Organization: ATHENA ({m.data_steward})"]},
        "packages": [{
            "name": m.source_name or m.source_id, "SPDXID": pkg, "versionInfo": m.version_tag,
            "downloadLocation": m.official_url if m.official_url.startswith("https://") else "NOASSERTION",
            "filesAnalyzed": bool(m.artifact_files), "licenseConcluded": "NOASSERTION",
            "licenseDeclared": m.license_spdx_id, "copyrightText": "NOASSERTION", "primaryPackagePurpose": "OTHER",
            "checksums": [{"algorithm": "SHA256", "checksumValue": m.sha256}],
            "comment": f"intended_use={m.intended_use}; origin_type={m.origin_type}; "
                       f"training_rights_approval={m.training_rights_approval}",
        }],
        "files": [], "relationships": [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
                                        "relatedSpdxElement": pkg}],
    }
    for i, f in enumerate(m.artifact_files[:max_files]):
        fid = f"SPDXRef-File-{i}"
        doc["files"].append({"fileName": f"./artifact/{f.path}", "SPDXID": fid, "licenseConcluded": "NOASSERTION",
                             "checksums": [{"algorithm": "SHA256", "checksumValue": f.sha256}]})
        doc["relationships"].append({"spdxElementId": pkg, "relationshipType": "CONTAINS", "relatedSpdxElement": fid})
    refs = sorted(set(re.findall(r"LicenseRef-[A-Za-z0-9.\-]+", m.license_spdx_id)))
    if refs:
        doc["hasExtractedLicensingInfos"] = [{"licenseId": r, "name": r, "extractedText": m.license_snapshot_text[:100_000]}
                                             for r in refs]
    return doc


def _cdx_license(spdx: str) -> list[dict]:
    if spdx == "NOASSERTION":
        return []
    if " WITH " in spdx or " AND " in spdx or " OR " in spdx:
        return [{"expression": spdx}]
    if spdx.startswith("LicenseRef-"):
        return [{"license": {"name": spdx}}]
    return [{"license": {"id": spdx}}]


def cyclonedx_bom(manifests: list[ProvenanceManifest]) -> dict:
    comps = []
    for m in manifests:
        comps.append({
            "type": "data", "bom-ref": f"{m.source_id}@{m.version_tag}#{m.sha256[:12]}",
            "name": m.source_name or m.source_id, "version": m.version_tag,
            "hashes": [{"alg": "SHA-256", "content": m.sha256}], "licenses": _cdx_license(m.license_spdx_id),
            "externalReferences": [{"type": "distribution", "url": m.official_url}],
            "data": [{"type": "dataset", "name": m.source_id, "classification": m.intended_use,
                      "governance": {"stewards": [{"organization": {"name": m.data_steward}}]}}],
            "properties": [{"name": f"athena:{k}", "value": str(v)} for k, v in (
                ("node_id", m.node_id), ("origin_type", m.origin_type), ("partition", m.partition),
                ("training_rights_approval", m.training_rights_approval),
                ("commercial_training_allowed", m.commercial_training_allowed), ("training_allowed", m.training_allowed),
                ("domain_transfer_warning", m.domain_transfer_warning), ("fetched_at_utc", _ts(m.fetched_at_utc)))
                if v is not None],
        })
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "serialNumber": f"urn:uuid:{uuid.uuid4()}", "version": 1,
            "metadata": {"timestamp": _ts(datetime.now(timezone.utc)),
                         "tools": {"components": [{"type": "application", "name": "athena-data-pipeline",
                                                   "version": TOOL.split("/")[1]}]}},
            "components": comps}


# ----------------------------------------------------------------------------------------------
# record indexes (for HC-12 contamination checks)
# ----------------------------------------------------------------------------------------------
def record_hash(obj: Any) -> str:
    if isinstance(obj, bytes):
        return sha256_bytes(obj)
    if isinstance(obj, str):
        return sha256_bytes(re.sub(r"\s+", " ", obj).strip().lower().encode())
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode())


def write_record_index(formatted_dir: Path, hashes: Iterable[str]) -> Path:
    path = Path(formatted_dir) / "record_index.json"
    path.write_text(json.dumps(sorted(set(hashes))), encoding="utf-8")
    return path


def load_record_index(artifact_dir: Path) -> set[str]:
    path = Path(artifact_dir) / "formatted" / "record_index.json"
    return set(json.loads(path.read_text())) if path.exists() else set()
