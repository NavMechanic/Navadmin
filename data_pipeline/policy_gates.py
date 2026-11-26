"""The 12 Hard Dataset & Model Promotion Controls (HC-01..HC-12) and the legal guardrails.

Everything that decides *whether* data may be fetched, where it may live, and whether it may
ever reach model training is here, so the rules have one source of truth:

* hard-coded source sets (non-commercial block, drop-zone-only portals/paid content, API-key
  gated live lookups, domain-boundary benchmarks) — the YAML registry cannot override them;
* the mode → partition map (train_candidates / rag_reference / eval_benchmarks /
  eval_noncommercial / private_ground_truth);
* artifact gates (``evaluate_artifact``) and the training-export gate (``evaluate_training_export``);
* the append-only, hash-chained training-rights ledger (the only way to reach ``APPROVED``).

Note: the source catalog and these 12 controls are a reconstruction — the reference workbook
was not available. See docs/DATA_PIPELINE.md.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .registry import Registry, SourceSpec
    from .storage import DataLake

# ----------------------------------------------------------------------------------------------
# vocabularies
# ----------------------------------------------------------------------------------------------
NODES = tuple(f"N{i}" for i in range(9))
INTENDED_USES = ("TRAIN-CANDIDATE", "RAG/REFERENCE", "LIVE-LOOKUP", "EVAL/BENCHMARK", "PRIVATE-GROUND-TRUTH")
ORIGIN_TYPES = ("PUBLIC_OPEN", "LIVE_API", "PRIVATE_GROUND_TRUTH", "SYNTHETIC_TEST_CASE")
APPROVAL_STATES = ("NOT_REVIEWED", "PENDING", "APPROVED", "REJECTED")
PARTITIONS = ("train_candidates", "rag_reference", "eval_benchmarks", "eval_noncommercial", "private_ground_truth")
ACCESS_METHODS = ("git", "http_archive", "api", "drop_zone", "vault")

HARD_CONTROLS: dict[str, str] = {
    "HC-01": "Provenance completeness: sidecar manifest with all required fields, license snapshot and SPDX id",
    "HC-02": "Mode isolation: one partition per intended use; nothing outside data_lake/ or inside rag/vectorstore",
    "HC-03": "Training rights: export to training only for TRAIN-CANDIDATE with a steward-ledger APPROVED decision",
    "HC-04": "Non-commercial block: NC/unasserted licenses (SRC-038/039 hard-coded) stay in eval_noncommercial, never trained",
    "HC-05": "No scraping: portals and paid/copyrighted content only via manual drop-zone with hash + entitlement",
    "HC-06": "Live lookups: env-var credentials only, TTL cache, fail closed, training_allowed = False",
    "HC-07": "Domain boundary: benchmark-only datasets are never electronics field proof or training data",
    "HC-08": "Private ground truth: internal:// vaults are never fetched over the network; schema-validated intake",
    "HC-09": "Synthetic isolation: SYNTHETIC_TEST_CASE data is never exported to training or relabelled",
    "HC-10": "Integrity & immutability: SHA-256 verified; manifests write-once; tampering detected",
    "HC-11": "Transport safety: HTTPS + TLS verification, MIME sniffing, size limits, safe archive extraction",
    "HC-12": "Leakage: temporal splits do not overlap; evaluation content never appears in a training export",
}

# ----------------------------------------------------------------------------------------------
# hard-coded guardrail sets (the registry is validated against these; it cannot relax them)
# ----------------------------------------------------------------------------------------------
NONCOMMERCIAL_HARD_BLOCK: dict[str, str] = {"SRC-038": "CC-BY-NC-SA-4.0", "SRC-039": "CC-BY-NC-SA-4.0"}  # MVTec AD / AD 2
DROP_ZONE_ONLY: frozenset[str] = frozenset({"SRC-013", "SRC-055", "SRC-059", "SRC-027", "SRC-050"})
API_KEY_LIVE_LOOKUPS: dict[str, tuple[str, ...]] = {
    "SRC-011": ("COMTRADE_API_KEY",), "SRC-049": ("COMTRADE_API_KEY",),           # UN Comtrade
    "SRC-014": ("SAM_GOV_API_KEY",), "SRC-054": ("SAM_GOV_API_KEY",),             # SAM.gov
    "SRC-045": ("NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"),                        # Nexar
    "SRC-064": ("NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"),
    "SRC-046": ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"),                    # DigiKey
    "SRC-047": ("MOUSER_API_KEY",),                                               # Mouser
    "SRC-060": ("FRED_API_KEY",),                                                 # FRED
}
DOMAIN_TRANSFER_WARNING = "BENCHMARK_ONLY_NOT_ELECTRONICS_PROOF"
DOMAIN_BOUNDARY_SOURCES: frozenset[str] = frozenset({"SRC-041", "SRC-067", "SRC-068", "SRC-070", "SRC-069"})
EXPECTED_VAULT_COUNT = 15
EXPECTED_SOURCE_IDS = tuple(f"SRC-{i:03d}" for i in range(1, 76))

# licenses considered safe for *commercial* model training once a steward approves the artifact
COMMERCIAL_TRAINING_LICENSES = frozenset({
    "CC0-1.0", "CC-BY-4.0", "CC-BY-3.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "Unlicense",
    "LicenseRef-PD-USGov", "NIST-PD", "AFL-3.0", "PDDL-1.0", "ODC-By-1.0",
})
_SPDX_ID = re.compile(r"^(NOASSERTION|LicenseRef-[A-Za-z0-9.\-]+|[A-Za-z0-9.\-+]+( WITH [A-Za-z0-9.\-]+)?)$")
_SOURCE_ID = re.compile(r"^SRC-\d{3}$")
_NODE = re.compile(r"^N[0-8]$")
_SAFE_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,95}$")


def is_noncommercial_license(spdx_id: str, flagged: bool = False) -> bool:
    """CC *-NC-* licenses, unasserted licenses and explicitly flagged LicenseRefs are non-commercial."""
    return flagged or spdx_id == "NOASSERTION" or "-NC" in spdx_id.upper()


def valid_spdx_expression(spdx_id: str) -> bool:
    return bool(_SPDX_ID.match(spdx_id or ""))


def partition_for(spec: "SourceSpec") -> str:
    """Intended use → storage partition. Non-commercial evaluation data is quarantined separately."""
    use = spec.intended_use
    if spec.source_id in NONCOMMERCIAL_HARD_BLOCK or is_noncommercial_license(spec.license_spdx_id, spec.noncommercial):
        return "eval_noncommercial"
    return {"TRAIN-CANDIDATE": "train_candidates", "RAG/REFERENCE": "rag_reference", "LIVE-LOOKUP": "rag_reference",
            "EVAL/BENCHMARK": "eval_benchmarks", "PRIVATE-GROUND-TRUTH": "private_ground_truth"}[use]


def commercial_training_allowed(spec: "SourceSpec") -> bool:
    """Hard-coded False for the NC block; otherwise only permissive licenses on open TRAIN-CANDIDATE sources.

    Share-alike licenses (e.g. KiCad's CC-BY-SA-4.0 WITH KiCad-libraries-exception) are deliberately not
    on the automatic list: they need a legal review before any weights are trained on them.
    """
    if spec.source_id in NONCOMMERCIAL_HARD_BLOCK:
        return False
    return (spec.intended_use == "TRAIN-CANDIDATE"
            and spec.access in ("git", "http_archive", "api")
            and spec.source_id not in API_KEY_LIVE_LOOKUPS
            and spec.source_id not in DROP_ZONE_ONLY
            and spec.source_id not in DOMAIN_BOUNDARY_SOURCES
            and not is_noncommercial_license(spec.license_spdx_id, spec.noncommercial)
            and spec.license_spdx_id in COMMERCIAL_TRAINING_LICENSES)


def safe_component(value: str, what: str) -> str:
    """A single path component; rejects traversal, separators and odd characters (HC-02)."""
    if what == "node_id" and not _NODE.match(value or ""):
        raise PolicyViolation("HC-02", f"invalid node id {value!r}")
    if what == "source_id" and not _SOURCE_ID.match(value or ""):
        raise PolicyViolation("HC-02", f"invalid source id {value!r}")
    if what not in ("node_id", "source_id") and not _SAFE_TAG.match(value or ""):
        raise PolicyViolation("HC-02", f"unsafe {what} {value!r}")
    return value


def sanitize_tag(value: str) -> str:
    tag = re.sub(r"[^A-Za-z0-9._+\-]", "-", value or "").strip("-.") or "untagged"
    return tag[:96]


# ----------------------------------------------------------------------------------------------
# violations and reports
# ----------------------------------------------------------------------------------------------
class PolicyViolation(Exception):
    def __init__(self, code: str, message: str, source_id: Optional[str] = None) -> None:
        assert code in HARD_CONTROLS, code
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.source_id = code, message, source_id


class GateFailed(Exception):
    def __init__(self, report: "GateReport") -> None:
        super().__init__("; ".join(str(v) for v in report.violations))
        self.report = report

    @property
    def codes(self) -> set[str]:
        return self.report.codes


@dataclass
class GateReport:
    subject: str
    controls: dict[str, str] = field(default_factory=lambda: {c: "N/A" for c in HARD_CONTROLS})
    violations: list[PolicyViolation] = field(default_factory=list)

    def check(self, code: str, ok: bool, message: str) -> bool:
        if ok:
            if self.controls[code] != "FAIL":
                self.controls[code] = "PASS"
        else:
            self.controls[code] = "FAIL"
            self.violations.append(PolicyViolation(code, message))
        return ok

    def fail(self, code: str, message: str) -> None:
        self.check(code, False, message)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def codes(self) -> set[str]:
        return {v.code for v in self.violations}

    def raise_if_failed(self) -> "GateReport":
        if self.violations:
            raise GateFailed(self)
        return self

    def to_dict(self) -> dict:
        return {"subject": self.subject, "ok": self.ok, "controls": dict(self.controls),
                "violations": [{"code": v.code, "message": v.message} for v in self.violations]}


# ----------------------------------------------------------------------------------------------
# registry-level checks (run by Registry on load; a violating catalog does not load)
# ----------------------------------------------------------------------------------------------
_SECRET_NAMES = re.compile(r"(?i)^(api[_-]?key|apikey|key|token|access[_-]?token|client[_-]?secret|secret|password|"
                           r"authorization|ocp-apim-subscription-key|x-api-key)$")


def _secret_literals(obj: Any, path: str = "") -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if _SECRET_NAMES.match(str(k)) and isinstance(v, str) and not v.startswith("env:"):
                found.append(p)
            found += _secret_literals(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _secret_literals(v, f"{path}[{i}]")
    return found


def check_registry_entry(spec: "SourceSpec") -> list[PolicyViolation]:
    v: list[PolicyViolation] = []

    def bad(code, msg):
        v.append(PolicyViolation(code, msg, spec.source_id))

    sid = spec.source_id
    if not valid_spdx_expression(spec.license_spdx_id):
        bad("HC-01", f"license_spdx_id {spec.license_spdx_id!r} is not an SPDX expression or LicenseRef")
    # HC-04 non-commercial
    if sid in NONCOMMERCIAL_HARD_BLOCK:
        if spec.license_spdx_id != NONCOMMERCIAL_HARD_BLOCK[sid]:
            bad("HC-04", f"{sid} must carry {NONCOMMERCIAL_HARD_BLOCK[sid]}")
        if spec.commercial_training_allowed is not False:
            bad("HC-04", f"{sid} is hard-coded commercial_training_allowed = False")
        if spec.intended_use != "EVAL/BENCHMARK" or spec.node_id != "N4":
            bad("HC-04", f"{sid} may only be EVAL/BENCHMARK at N4 (eval_noncommercial/N4)")
    if is_noncommercial_license(spec.license_spdx_id, spec.noncommercial):
        if spec.intended_use != "EVAL/BENCHMARK":
            bad("HC-04", f"non-commercial/unasserted license {spec.license_spdx_id} can only be EVAL/BENCHMARK")
        if spec.commercial_training_allowed or spec.training_allowed:
            bad("HC-04", "non-commercial license cannot allow training")
    if spec.commercial_training_allowed and not commercial_training_allowed(spec):
        bad("HC-03", f"commercial_training_allowed=True is not permitted for {spec.license_spdx_id}/{spec.intended_use}")
    if spec.training_allowed and spec.intended_use != "TRAIN-CANDIDATE":
        bad("HC-03", f"training_allowed=True requires TRAIN-CANDIDATE (is {spec.intended_use})")
    # HC-05 drop-zone only
    if sid in DROP_ZONE_ONLY and spec.access != "drop_zone":
        bad("HC-05", f"{sid} is a portal/licensed source and may only use the manual drop-zone adapter")
    if spec.access == "drop_zone" and (spec.training_allowed or spec.intended_use == "TRAIN-CANDIDATE"):
        bad("HC-05", "manual/licensed drop-zone content is never a training candidate")
    # HC-06 live lookups
    if sid in API_KEY_LIVE_LOOKUPS:
        if spec.access != "api" or spec.intended_use != "LIVE-LOOKUP":
            bad("HC-06", f"{sid} must be an api LIVE-LOOKUP")
        if spec.training_allowed is not False:
            bad("HC-06", f"{sid} is a live lookup: training_allowed must be False")
        envs = set(spec.api.auth.env_vars()) if spec.api else set()
        missing = set(API_KEY_LIVE_LOOKUPS[sid]) - envs
        if missing:
            bad("HC-06", f"{sid} must authenticate via env vars {sorted(missing)}")
        if spec.api and not spec.api.ttl_seconds:
            bad("HC-06", f"{sid} must cache with a TTL")
    if spec.intended_use == "LIVE-LOOKUP" and spec.training_allowed:
        bad("HC-06", "LIVE-LOOKUP data is never training data")
    secrets = _secret_literals(spec.model_dump(mode="json"))
    if secrets:
        bad("HC-06", f"credential literals in registry config at {secrets}; reference env vars as 'env:NAME'")
    # HC-07 domain boundary
    if sid in DOMAIN_BOUNDARY_SOURCES:
        if spec.domain_transfer_warning != DOMAIN_TRANSFER_WARNING:
            bad("HC-07", f"{sid} must be tagged domain_transfer_warning = {DOMAIN_TRANSFER_WARNING}")
        if spec.intended_use != "EVAL/BENCHMARK" or spec.training_allowed:
            bad("HC-07", f"{sid} is benchmark-only")
    # HC-08 private ground truth
    is_internal = spec.official_url.startswith("internal://athena/")
    if is_internal != (spec.access == "vault"):
        bad("HC-08", "internal://athena/* URIs are vault-only and vaults must use internal://athena/*")
    if spec.access == "vault" and (spec.intended_use != "PRIVATE-GROUND-TRUTH" or spec.origin_type != "PRIVATE_GROUND_TRUTH"):
        bad("HC-08", "vault sources are PRIVATE-GROUND-TRUTH with origin PRIVATE_GROUND_TRUTH")
    # HC-11 transport
    if not is_internal and not spec.official_url.startswith("https://"):
        bad("HC-11", f"official_url must be https:// (got {spec.official_url})")
    for url in spec.network_urls():
        if not url.startswith("https://"):
            bad("HC-11", f"fetch URL must be https:// (got {url})")
    return v


# ----------------------------------------------------------------------------------------------
# training-rights ledger (append-only, hash-chained)
# ----------------------------------------------------------------------------------------------
class TrainingRightsLedger:
    """``_governance/training_rights_ledger.jsonl``. The only place an APPROVED decision can come from."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows, prev = [], "0" * 64
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            row = json.loads(line)
            body = {k: v for k, v in row.items() if k != "entry_hash"}
            if row.get("prev_hash") != prev or _hash(body) != row.get("entry_hash"):
                raise PolicyViolation("HC-10", f"training-rights ledger tampered at line {n}")
            prev = row["entry_hash"]
            rows.append(row)
        return rows

    def record(self, *, source_id: str, sha256: str, decision: str, steward_id: str, registry: "Registry",
               rationale: str = "", now: Optional[datetime] = None) -> dict:
        spec = registry.get(source_id)
        if decision not in ("APPROVED", "REJECTED", "PENDING"):
            raise PolicyViolation("HC-03", f"decision must be APPROVED/REJECTED/PENDING, got {decision!r}")
        if steward_id != spec.data_steward:
            raise PolicyViolation("HC-03", f"{steward_id} is not the data steward of {source_id} ({spec.data_steward})")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
            raise PolicyViolation("HC-10", "decision must reference the artifact's SHA-256")
        with self._lock:
            rows = self.entries()
            body = {"source_id": source_id, "sha256": sha256, "decision": decision, "steward_id": steward_id,
                    "rationale": rationale, "decided_at_utc": (now or datetime.now(timezone.utc)).isoformat(),
                    "prev_hash": rows[-1]["entry_hash"] if rows else "0" * 64}
            row = {**body, "entry_hash": _hash(body)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
            return row

    def effective(self, source_id: str, sha256: str) -> str:
        decision = "NOT_REVIEWED"
        for row in self.entries():
            if row["source_id"] == source_id and row["sha256"] == sha256:
                decision = row["decision"]
        return decision


def _hash(obj: dict) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


# ----------------------------------------------------------------------------------------------
# artifact gate (HC-01, 02, 04–11) and training-export gate (adds HC-03, HC-12)
# ----------------------------------------------------------------------------------------------
def evaluate_artifact(lake: "DataLake", artifact_dir: Path, registry: "Registry",
                      secrets: Iterable[str] = ()) -> GateReport:
    from .provenance import MANIFEST_NAME, ManifestError, load_manifest, verify_payload

    artifact_dir = Path(artifact_dir)
    report = GateReport(str(artifact_dir))
    try:
        manifest = load_manifest(artifact_dir)
    except ManifestError as exc:
        report.fail("HC-01", str(exc))
        return report
    try:
        spec = registry.get(manifest.source_id)
    except KeyError:
        report.fail("HC-01", f"{manifest.source_id} is not in the source registry")
        return report

    # HC-01 provenance completeness and agreement with the registry
    report.check("HC-01", manifest.node_id == spec.node_id, f"node_id {manifest.node_id} != registry {spec.node_id}")
    report.check("HC-01", manifest.intended_use == spec.intended_use,
                 f"intended_use {manifest.intended_use} != registry {spec.intended_use}")
    report.check("HC-01", manifest.license_spdx_id == spec.license_spdx_id,
                 f"license_spdx_id {manifest.license_spdx_id} != registry {spec.license_spdx_id}")
    report.check("HC-01", manifest.official_url == spec.official_url, "official_url differs from the registry")
    report.check("HC-01", manifest.data_steward == spec.data_steward, "data_steward differs from the registry")
    report.check("HC-01", bool(manifest.license_snapshot_text.strip()), "license snapshot is empty")

    # HC-02 mode isolation
    expected = partition_for(spec)
    try:
        located = lake.partition_of(artifact_dir)
    except PolicyViolation as exc:
        report.fail("HC-02", exc.message)
        located = None
    report.check("HC-02", located == expected, f"artifact stored in {located}, but {spec.intended_use} belongs in {expected}")
    report.check("HC-02", manifest.partition in (None, expected), f"manifest partition {manifest.partition} != {expected}")
    report.check("HC-02", lake.node_of(artifact_dir) == spec.node_id, "artifact stored under the wrong node directory")

    # HC-04 non-commercial
    if spec.source_id in NONCOMMERCIAL_HARD_BLOCK or is_noncommercial_license(spec.license_spdx_id, spec.noncommercial):
        report.check("HC-04", located == "eval_noncommercial", f"non-commercial {spec.source_id} outside eval_noncommercial")
        report.check("HC-04", manifest.commercial_training_allowed is False, "non-commercial data marked trainable")

    # HC-05 drop-zone provenance
    if spec.access == "drop_zone":
        report.check("HC-05", manifest.fetch_method == "manual_drop_zone", "drop-zone source not acquired via intake")
        report.check("HC-05", manifest.entitlement is not None, "drop-zone artifact without entitlement metadata")

    # HC-06 live / API credentials never persisted
    if spec.intended_use == "LIVE-LOOKUP":
        report.check("HC-06", manifest.training_allowed is False, "live-lookup artifact not tagged training_allowed=False")
        report.check("HC-06", manifest.origin_type == "LIVE_API", "live-lookup artifact must be origin LIVE_API")
    secret_values = [s for s in secrets if s and len(s) >= 6]
    if secret_values:
        blobs = [(artifact_dir / MANIFEST_NAME).read_bytes()] + [p.read_bytes() for p in artifact_dir.rglob("*")
                                                                if p.is_file() and p.stat().st_size < 50_000_000]
        leaked = any(s.encode() in b for s in secret_values for b in blobs)
        report.check("HC-06", not leaked, "a credential value is present in the stored artifact or manifest")

    # HC-07 domain boundary
    if spec.source_id in DOMAIN_BOUNDARY_SOURCES:
        report.check("HC-07", manifest.domain_transfer_warning == DOMAIN_TRANSFER_WARNING,
                     "benchmark-only dataset missing its domain_transfer_warning")

    # HC-08 private ground truth / HC-09 synthetic
    in_fixtures = "synthetic_fixtures" in artifact_dir.parts
    if spec.access == "vault":
        report.check("HC-08", manifest.origin_type in ("PRIVATE_GROUND_TRUTH", "SYNTHETIC_TEST_CASE"),
                     f"vault artifact with origin {manifest.origin_type}")
        report.check("HC-08", manifest.fetch_method in ("vault_intake", "synthetic_fixture"),
                     "vault artifact not produced by the vault intake")
    elif manifest.origin_type == "PRIVATE_GROUND_TRUTH":
        report.fail("HC-08", "PRIVATE_GROUND_TRUTH origin outside a vault source")
    if in_fixtures or manifest.origin_type == "SYNTHETIC_TEST_CASE" or _has_synthetic_marker(artifact_dir):
        report.check("HC-09", manifest.origin_type == "SYNTHETIC_TEST_CASE",
                     "synthetic fixture data relabelled as a non-synthetic origin")
        report.check("HC-09", manifest.training_allowed is False, "synthetic test cases must not allow training")

    # HC-10 integrity
    ok, msg = verify_payload(artifact_dir, manifest)
    report.check("HC-10", ok, msg)

    # HC-11 transport (recorded at fetch time)
    if spec.access not in ("vault", "drop_zone"):
        report.check("HC-11", (manifest.transport or {}).get("tls_verified") is True, "artifact not fetched over verified TLS")
    return report


def _has_synthetic_marker(artifact_dir: Path) -> bool:
    for p in (artifact_dir / "artifact").glob("*.jsonl") if (artifact_dir / "artifact").exists() else []:
        with p.open(encoding="utf-8") as fh:
            first = fh.readline()
        if '"_synthetic": true' in first:
            return True
    return False


def evaluate_training_export(lake: "DataLake", artifact_dir: Path, registry: "Registry", ledger: TrainingRightsLedger,
                             eval_hashes: Optional[set[str]] = None, secrets: Iterable[str] = ()) -> GateReport:
    from .provenance import load_manifest

    report = evaluate_artifact(lake, artifact_dir, registry, secrets)
    if "HC-01" in report.codes and not (Path(artifact_dir) / "provenance_manifest.json").exists():
        return report
    manifest = load_manifest(artifact_dir)
    spec = registry.get(manifest.source_id)
    report.subject = f"training-export:{artifact_dir}"

    nc = spec.source_id in NONCOMMERCIAL_HARD_BLOCK or is_noncommercial_license(spec.license_spdx_id, spec.noncommercial)
    report.check("HC-04", not nc, f"{spec.source_id} is non-commercial ({spec.license_spdx_id}); never exported to training")
    report.check("HC-06", spec.intended_use != "LIVE-LOOKUP", "LIVE-LOOKUP data is runtime grounding only")
    report.check("HC-07", spec.source_id not in DOMAIN_BOUNDARY_SOURCES,
                 f"{spec.source_id} is {DOMAIN_TRANSFER_WARNING}; not training or field evidence")
    report.check("HC-09", manifest.origin_type != "SYNTHETIC_TEST_CASE", "synthetic test cases are never training data")
    report.check("HC-03", spec.intended_use == "TRAIN-CANDIDATE" and lake.partition_of(artifact_dir) == "train_candidates",
                 f"only TRAIN-CANDIDATE artifacts in train_candidates can be exported (is {spec.intended_use})")
    report.check("HC-03", commercial_training_allowed(spec) and manifest.commercial_training_allowed is True,
                 f"{spec.source_id} is not cleared for commercial training ({spec.license_spdx_id}, {spec.intended_use}, "
                 f"{spec.access})")
    decision = ledger.effective(spec.source_id, manifest.sha256)
    report.check("HC-03", decision == "APPROVED",
                 f"training_rights_approval is {decision} in the steward ledger (manifest says "
                 f"{manifest.training_rights_approval}); export requires APPROVED")
    if eval_hashes is not None and lake.partition_of(artifact_dir) == "train_candidates":
        from .provenance import load_record_index
        overlap = load_record_index(artifact_dir) & eval_hashes
        report.check("HC-12", not overlap, f"{len(overlap)} training records also appear in evaluation partitions")
    split_meta = Path(artifact_dir) / "formatted" / "splits.json"
    if split_meta.exists():
        try:
            check_temporal_split(json.loads(split_meta.read_text()))
        except PolicyViolation as exc:
            report.fail("HC-12", exc.message)
    return report


def check_temporal_split(splits: dict) -> None:
    """``{"train": {"start":..,"end":..}, "val": {...}, "test": {...}}`` with ISO-8601 UTC or integer bounds."""
    order = [k for k in ("train", "val", "test") if k in splits and splits[k]]

    def val(x):
        return datetime.fromisoformat(x) if isinstance(x, str) else x

    for a, b in zip(order, order[1:]):
        if val(splits[a]["end"]) >= val(splits[b]["start"]):
            raise PolicyViolation("HC-12", f"temporal leakage: {a} ends {splits[a]['end']} at/after {b} starts {splits[b]['start']}")
    for k in order:
        if val(splits[k]["start"]) > val(splits[k]["end"]):
            raise PolicyViolation("HC-12", f"split {k} has start after end")
    groups = [set(splits[k].get("groups") or []) for k in order]
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if groups[i] & groups[j]:
                raise PolicyViolation("HC-12", f"groups {sorted(groups[i] & groups[j])[:5]} appear in {order[i]} and {order[j]}")
