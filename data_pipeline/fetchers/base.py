"""BaseFetcher: retry, per-host rate limiting, TLS enforcement, MIME sniffing, size limits,
dry-run planning, staging, provenance and gating.

Lifecycle of ``fetch()``::

    preflight()            guardrails that need no network (access type, drop-zone-only, vault, TLS, creds)
    dry_run?  -> PLANNED   nothing is downloaded
    _acquire(staging)      subclass downloads into <lake>/_staging/<SRC>-xxxx/artifact
    digest + manifest      SHA-256 (file or tree), license snapshot, ProvenanceManifest
    evaluate_artifact()    HC-01..HC-11 on the staged copy — a failing artifact is never promoted
    promote()              atomic move to <partition>/<node>/<SRC>/<version>__<sha12>/
"""
from __future__ import annotations

import io
import os
import random
import shutil
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ClassVar, Iterable, Optional
from urllib.parse import urlparse

import httpx

from ..policy_gates import (DOMAIN_BOUNDARY_SOURCES, DOMAIN_TRANSFER_WARNING, DROP_ZONE_ONLY, GateReport, PolicyViolation,
                            commercial_training_allowed, evaluate_artifact, partition_for, sanitize_tag)
from ..provenance import (ProvenanceManifest, html_to_text, payload_digest, redact, sha256_bytes, write_manifest_once)

USER_AGENT = "athena-data-pipeline/1.0 (+policy-gated research data acquisition)"
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


class MissingCredentialError(PolicyViolation):
    def __init__(self, source_id: str, env_vars: Iterable[str]) -> None:
        super().__init__("HC-06", f"{source_id} needs env var(s) {sorted(env_vars)}; refusing to call unauthenticated",
                         source_id)


@dataclass
class FetchOutcome:
    source_id: str
    status: str                       # PLANNED | STAGED | UNCHANGED | AWAITING_DROP | SKIPPED | REJECTED | FAILED
    artifact_dir: Optional[Path] = None
    manifest: Optional[ProvenanceManifest] = None
    reasons: list[str] = field(default_factory=list)
    report: Optional[GateReport] = None
    plan: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "status": self.status,
                "partition": self.manifest.partition if self.manifest else self.plan.get("partition"),
                "artifact_dir": str(self.artifact_dir) if self.artifact_dir else None,
                "sha256": self.manifest.sha256 if self.manifest else None, "reasons": self.reasons,
                "gate": self.report.to_dict() if self.report else None, "plan": self.plan}


# ----------------------------------------------------------------------------------------------
# MIME sniffing
# ----------------------------------------------------------------------------------------------
def sniff_mime(head: bytes, name: str = "") -> str:
    h = head.lstrip(b"\xef\xbb\xbf")
    if h.startswith(b"PK\x03\x04") or h.startswith(b"PK\x05\x06"):
        return "application/zip"
    if h.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if h.startswith(b"\xfd7zXZ\x00"):
        return "application/x-xz"
    if len(head) > 262 and head[257:262] == b"ustar":
        return "application/x-tar"
    if h.startswith(b"%PDF-"):
        return "application/pdf"
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h.startswith(b"PAR1"):
        return "application/vnd.apache.parquet"
    if h.startswith(b"\x89HDF\r\n\x1a\n"):
        return "application/x-hdf5"
    if h.startswith(b"\x93NUMPY"):
        return "application/x-npy"
    if h.startswith(b"MATLAB"):
        return "application/x-matlab-data"
    if h.startswith(b"ISO-10303-21"):
        return "model/step"
    try:
        text = h[:4096].decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < min(len(h), 4096) - 4:          # invalid byte well inside the sample: binary
            return "application/octet-stream"
        text = h[:exc.start].decode("utf-8")           # sample cut through a multi-byte character
    t = text.lstrip().lower()
    if t.startswith("<!doctype html") or t.startswith("<html") or "<html" in t[:512] or "<head" in t[:512]:
        return "text/html"
    if t.startswith("<?xml") or t.startswith("<"):
        return "application/xml"
    if t.startswith("{") or t.startswith("["):
        return "application/json"
    if t.startswith("@prefix") or t.startswith("prefix ") or t.startswith("@base"):
        return "text/turtle"
    if name.lower().endswith(".csv") or ("," in t.split("\n", 1)[0] and "\n" in t):
        return "text/csv"
    return "text/plain"


def mime_compatible(actual: str, expected: list[str]) -> bool:
    if not expected:
        return True
    aliases = {"text/xml": "application/xml", "application/x-gzip": "application/gzip"}
    a = aliases.get(actual, actual)
    exp = {aliases.get(e, e) for e in expected}
    if a in exp:
        return True
    if a == "text/plain" and exp & {"text/csv", "text/turtle", "application/x-arff"}:
        return True
    if a == "text/csv" and "text/plain" in exp:
        return True
    return False


# ----------------------------------------------------------------------------------------------
# rate limiting
# ----------------------------------------------------------------------------------------------
class HostRateLimiter:
    """Minimum interval between requests to the same host (shared across fetchers in a process)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.clock, self.sleep = clock, sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, per_second: float) -> None:
        interval = 1.0 / per_second
        with self._lock:
            now = self.clock()
            due = self._next.get(host, now)
            delay = max(0.0, due - now)
            self._next[host] = max(now, due) + interval
        if delay:
            self.sleep(delay)


DEFAULT_RATE_LIMITER = HostRateLimiter()


# ----------------------------------------------------------------------------------------------
# safe archive extraction (HC-11)
# ----------------------------------------------------------------------------------------------
def _safe_member(name: str) -> PurePosixPath:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or (p.parts and ":" in p.parts[0]):
        raise PolicyViolation("HC-11", f"archive member {name!r} escapes the extraction directory")
    return p


def safe_extract(archive: Path, dest: Path, max_bytes: int, max_members: int = 500_000) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    total, count = 0, 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                rel = _safe_member(info.filename)
                count += 1
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PolicyViolation("HC-11", f"archive member {info.filename!r} is a symlink")
                total += info.file_size
                if total > max_bytes or count > max_members:
                    raise PolicyViolation("HC-11", "archive expands beyond the size/member limit (zip bomb guard)")
                target = dest / rel
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        return count
    with tarfile.open(archive) as tf:
        for m in tf.getmembers():
            rel = _safe_member(m.name)
            count += 1
            if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                raise PolicyViolation("HC-11", f"archive member {m.name!r} is a link/device")
            total += max(m.size, 0)
            if total > max_bytes or count > max_members:
                raise PolicyViolation("HC-11", "archive expands beyond the size/member limit")
            target = dest / rel
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return count


# ----------------------------------------------------------------------------------------------
# BaseFetcher
# ----------------------------------------------------------------------------------------------
class BaseFetcher:
    access: ClassVar[tuple[str, ...]] = ()
    fetch_method: ClassVar[str] = ""
    uses_network: ClassVar[bool] = True

    def __init__(self, spec, lake, registry, *, client: Optional[httpx.Client] = None, dry_run: bool = False,
                 env: Optional[dict] = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 rate_limiter: HostRateLimiter = DEFAULT_RATE_LIMITER, retries: int = 3, backoff_s: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep, timeout_s: float = 60.0,
                 max_bytes: Optional[int] = None) -> None:
        self.spec, self.lake, self.registry = spec, lake, registry
        self.dry_run, self.env = dry_run, (env if env is not None else dict(os.environ))
        self.now, self.rate_limiter, self.retries, self.backoff_s, self.sleep = now, rate_limiter, retries, backoff_s, sleep
        self.max_bytes = max_bytes or spec.max_bytes
        self.transport_log: dict[str, Any] = {"tls_verified": None, "requests": 0}
        if self.spec.access not in self.access:
            raise PolicyViolation("HC-05" if spec.source_id in DROP_ZONE_ONLY or spec.access == "drop_zone" else
                                  "HC-08" if spec.access == "vault" else "HC-02",
                                  f"{type(self).__name__} cannot acquire {spec.source_id} (access={spec.access})",
                                  spec.source_id)
        self._own_client = False
        if self.uses_network:
            if client is None:
                client = httpx.Client(timeout=timeout_s, follow_redirects=True, verify=True,
                                      headers={"User-Agent": USER_AGENT})
                self._own_client = True
            self.client = client
        else:
            self.client = None

    # -- guardrails ----------------------------------------------------------------------------
    def preflight(self) -> None:
        s = self.spec
        if s.source_id in DROP_ZONE_ONLY and self.uses_network:
            raise PolicyViolation("HC-05", f"{s.source_id} may only be acquired through the manual drop zone", s.source_id)
        if s.official_url.startswith("internal://") and self.uses_network:
            raise PolicyViolation("HC-08", f"{s.official_url} is a private vault; never fetched over the network", s.source_id)
        for url in s.network_urls() if self.uses_network else ():
            self.check_url(url)
        if self.uses_network and self.client is not None:
            verify = getattr(self.client, "_transport", None)
            ssl_ctx = getattr(getattr(verify, "_pool", None), "_ssl_context", None)
            if ssl_ctx is not None and getattr(ssl_ctx, "verify_mode", 2) == 0:
                raise PolicyViolation("HC-11", "TLS certificate verification is disabled on the HTTP client", s.source_id)

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme == "internal":
            raise PolicyViolation("HC-08", f"{url} is a private vault URI; network fetch refused", self.spec.source_id)
        if parsed.scheme != "https" or not parsed.hostname:
            raise PolicyViolation("HC-11", f"refusing non-HTTPS URL {url}", self.spec.source_id)

    def credentials(self) -> dict[str, str]:
        api = getattr(self.spec, "api", None)
        if not api:
            return {}
        names = api.auth.env_vars()
        values = {n: self.env.get(n, "") for n in names}
        missing = [n for n, v in values.items() if not v]
        if missing and not api.auth.optional:
            raise MissingCredentialError(self.spec.source_id, missing)
        return {k: v for k, v in values.items() if v}

    def secrets(self) -> list[str]:
        try:
            return list(self.credentials().values())
        except MissingCredentialError:
            return []

    # -- HTTP ----------------------------------------------------------------------------------
    def request(self, method: str, url: str, *, expected_mime: Optional[list[str]] = None, stream_to: Optional[Path] = None,
                **kwargs) -> httpx.Response:
        """Rate-limited, retried, TLS-checked request. With ``stream_to`` the body is written to disk (size-capped)."""
        self.check_url(url)
        host = urlparse(url).hostname
        attempt = 0
        while True:
            attempt += 1
            self.rate_limiter.wait(host, self.spec.rate_limit_per_s)
            try:
                if stream_to is None:
                    resp = self.client.request(method, url, **kwargs)
                    self.transport_log["requests"] += 1
                    if resp.status_code in TRANSIENT_STATUS and attempt <= self.retries:
                        self._backoff(attempt, resp)
                        continue
                    self._after_response(resp)
                    if len(resp.content) > self.max_bytes:
                        raise PolicyViolation("HC-11", f"response exceeds max_bytes={self.max_bytes}", self.spec.source_id)
                    return resp
                with self.client.stream(method, url, **kwargs) as resp:
                    self.transport_log["requests"] += 1
                    if resp.status_code in TRANSIENT_STATUS and attempt <= self.retries:
                        self._backoff(attempt, resp)
                        continue
                    self._after_response(resp)
                    written = 0
                    stream_to.parent.mkdir(parents=True, exist_ok=True)
                    with open(stream_to, "wb") as out:
                        for chunk in resp.iter_bytes(1 << 20):
                            written += len(chunk)
                            if written > self.max_bytes:
                                out.close()
                                stream_to.unlink(missing_ok=True)
                                raise PolicyViolation("HC-11", f"download exceeds max_bytes={self.max_bytes}",
                                                      self.spec.source_id)
                            out.write(chunk)
                    if expected_mime is not None:
                        with open(stream_to, "rb") as fh:
                            mime = sniff_mime(fh.read(8192), stream_to.name)
                        if not mime_compatible(mime, expected_mime):
                            raise PolicyViolation("HC-11", f"{url} returned {mime}, expected {expected_mime} "
                                                           "(error page, login wall or wrong file)", self.spec.source_id)
                        self.transport_log.setdefault("mime", {})[stream_to.name] = mime
                    return resp
            except httpx.TransportError as exc:
                if attempt <= self.retries:
                    self._backoff(attempt, None)
                    continue
                raise FetchError(f"{url}: {type(exc).__name__}: {exc}") from exc

    def _after_response(self, resp: httpx.Response) -> None:
        final = resp.url
        if final.scheme != "https":
            raise PolicyViolation("HC-11", f"redirected to non-HTTPS {final}", self.spec.source_id)
        self.transport_log.update(tls_verified=True, final_host=final.host, http_status=resp.status_code)
        if resp.status_code >= 400:
            raise FetchError(f"{self.spec.source_id}: HTTP {resp.status_code} from {final.host}")

    def _backoff(self, attempt: int, resp: Optional[httpx.Response]) -> None:
        retry_after = None
        if resp is not None:
            try:
                retry_after = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                retry_after = None
            resp.close()
        delay = retry_after if retry_after is not None else self.backoff_s * (2 ** (attempt - 1)) * (1 + random.random() * 0.1)
        self.sleep(min(delay, 120.0))

    # -- license snapshot ----------------------------------------------------------------------
    def license_snapshot(self, artifact_root: Optional[Path] = None) -> tuple[str, str]:
        """(text, where_from). Repo LICENSE file → license_url page → registry license_text. Fails closed."""
        if artifact_root is not None and artifact_root.exists():
            for pat in ("LICENSE*", "LICENCE*", "COPYING*", "license*"):
                for p in sorted(artifact_root.glob(pat)):
                    if p.is_file() and p.stat().st_size < 2_000_000:
                        text = p.read_text(encoding="utf-8", errors="replace").strip()
                        if text:
                            return text, f"artifact:{p.name}"
        if self.spec.license_url and self.uses_network and self.client is not None:
            try:
                resp = self.request("GET", self.spec.license_url)
                if resp.status_code != 200:          # 202/203 challenge or interstitial pages are not license text
                    raise FetchError(f"license page returned HTTP {resp.status_code}")
                body = resp.text
                text = html_to_text(body) if "html" in resp.headers.get("content-type", "") or "<html" in body[:2000].lower() else body
                if text.strip():
                    return text, self.spec.license_url
            except (FetchError, PolicyViolation):
                pass
        if self.spec.license_text:
            return self.spec.license_text, "registry:license_text"
        raise PolicyViolation("HC-01", f"no license snapshot obtainable for {self.spec.source_id}", self.spec.source_id)

    # -- template ------------------------------------------------------------------------------
    def plan(self) -> dict:
        return {"source_id": self.spec.source_id, "access": self.spec.access, "fetch_method": self.fetch_method,
                "partition": partition_for(self.spec), "node": self.spec.node_id, "urls": self.spec.network_urls(),
                "intended_use": self.spec.intended_use}

    def fetch(self) -> FetchOutcome:
        sid = self.spec.source_id
        try:
            self.preflight()
            if self.dry_run:
                return FetchOutcome(sid, "PLANNED", plan=self.plan())
            staging = self.lake.staging_dir(sid)
            try:
                payload = staging / "artifact"
                payload.mkdir()
                info = self._acquire(payload)
                if info.get("status"):
                    shutil.rmtree(staging, ignore_errors=True)
                    return FetchOutcome(sid, info["status"], reasons=info.get("reasons", []), plan=self.plan())
                return self._finalize(staging, info, info.get("final_parent"))
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        except PolicyViolation as exc:
            return FetchOutcome(sid, "REJECTED", reasons=[str(exc)])
        except FetchError as exc:
            return FetchOutcome(sid, "FAILED", reasons=[str(exc)])

    def close(self) -> None:
        if self._own_client and self.client is not None:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _acquire(self, payload_dir: Path) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def build_manifest(self, payload_dir: Path, info: dict) -> ProvenanceManifest:
        s = self.spec
        secrets = self.secrets()
        for p in payload_dir.rglob("*"):
            if p.is_file() and secrets and p.stat().st_size < 200_000_000:
                data, n = redact(p.read_bytes(), secrets)
                if n:
                    p.write_bytes(data)
                    info["redactions"] = info.get("redactions", 0) + n
        sha, files = payload_digest(payload_dir)
        lic_text, lic_from = info.get("license") or self.license_snapshot(payload_dir)
        return ProvenanceManifest(
            source_id=s.source_id, node_id=s.node_id, intended_use=s.intended_use, official_url=s.official_url,
            fetched_at_utc=info.get("fetched_at") or self.now(), license_snapshot_text=lic_text,
            license_spdx_id=s.license_spdx_id, sha256=sha, version_tag=info.get("version_tag") or s.version_tag,
            origin_type=info.get("origin_type") or s.origin_type, training_rights_approval="NOT_REVIEWED",
            data_steward=s.data_steward, source_name=s.name, partition=partition_for(s),
            fetch_method=info.get("fetch_method") or self.fetch_method, artifact_files=files[:20000],
            license_snapshot_sha256=sha256_bytes(lic_text.encode()), license_source=lic_from,
            commercial_training_allowed=commercial_training_allowed(s) and info.get("origin_type") != "SYNTHETIC_TEST_CASE",
            training_allowed=s.training_allowed and info.get("origin_type") != "SYNTHETIC_TEST_CASE",
            domain_transfer_warning=DOMAIN_TRANSFER_WARNING if s.source_id in DOMAIN_BOUNDARY_SOURCES else s.domain_transfer_warning,
            catalog_status=s.catalog_status, entitlement=info.get("entitlement"),
            transport=dict(self.transport_log) if self.uses_network else None,
            cache_expires_at_utc=info.get("cache_expires_at"), query_key=info.get("query_key"),
            redactions=info.get("redactions", 0),
        )

    def _finalize(self, staging: Path, info: dict, final_parent: Optional[Path] = None) -> FetchOutcome:
        s = self.spec
        manifest = self.build_manifest(staging / "artifact", info)
        write_manifest_once(staging, manifest, self.secrets())
        if final_parent is not None:
            final = self.lake.ensure_inside(final_parent / f"{sanitize_tag(manifest.version_tag)}__{manifest.sha256[:12]}",
                                            self.lake.source_dir(s))
        else:
            final = self.lake.version_dir(s, manifest.version_tag, manifest.sha256)
        if final.exists():
            existing = (final / "provenance_manifest.json")
            if existing.exists():
                from ..provenance import load_manifest
                old = load_manifest(final)
                if old.sha256 == manifest.sha256:
                    report = evaluate_artifact(self.lake, final, self.registry, self.secrets())
                    return FetchOutcome(s.source_id, "UNCHANGED", final, old, report=report)
            raise PolicyViolation("HC-10", f"{final} exists with different content; immutable artifacts are never replaced")
        self.lake.promote(staging, final)
        report = evaluate_artifact(self.lake, final, self.registry, self.secrets())
        if not report.ok:
            rejected = self.lake.source_dir(s) / "_rejected" / final.name
            rejected.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(final), str(rejected))
            return FetchOutcome(s.source_id, "REJECTED", rejected, manifest, [str(v) for v in report.violations], report)
        return FetchOutcome(s.source_id, "STAGED", final, manifest, report=report)
