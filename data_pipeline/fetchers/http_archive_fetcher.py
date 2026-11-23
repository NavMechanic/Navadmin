"""Direct download + checksum verifier (JSON Schema, PROV-O, NIST SI / AI RMF / AM-Bench, NASA SE &
PCoE, Paderborn, OPSD, GDELT, Open Supply Hub, RBI, EUR-Lex, ECHA, Zenodo datasets).

Each file is streamed over HTTPS with a size cap, MIME-sniffed against ``expected_mime`` (an HTML
error/login page in place of a ZIP or JSON is rejected), verified against ``expected_sha256`` when
the registry pins one, and optionally extracted with the safe extractor (no traversal, symlinks,
devices or zip bombs). The raw download is kept next to the extraction so the hash stays verifiable.
"""
from __future__ import annotations

from pathlib import Path

from ..policy_gates import PolicyViolation
from ..provenance import sha256_file
from .base import BaseFetcher, safe_extract


class HttpArchiveFetcher(BaseFetcher):
    access = ("http_archive",)
    fetch_method = "http_archive"

    def plan(self) -> dict:
        return {**super().plan(), "files": [f.model_dump() for f in self.spec.http.files]}

    def _acquire(self, payload: Path) -> dict:
        pinned_ok = []
        for f in self.spec.http.files:
            target = payload / f.name
            self.request("GET", f.url, stream_to=target, expected_mime=f.expected_mime or self.spec.expected_mime)
            digest = sha256_file(target)
            if f.expected_sha256 and digest != f.expected_sha256:
                target.unlink(missing_ok=True)
                raise PolicyViolation("HC-10", f"{f.name}: SHA-256 {digest} does not match the pinned "
                                               f"{f.expected_sha256}", self.spec.source_id)
            pinned_ok.append(bool(f.expected_sha256))
            if f.extract != "none":
                safe_extract(target, payload / f"{f.name}.d", self.max_bytes)
        self.transport_log["checksums_pinned"] = all(pinned_ok)
        return {"fetch_method": "http_archive"}
