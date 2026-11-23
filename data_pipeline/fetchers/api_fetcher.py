"""Authenticated REST / GraphQL clients (TED, OpenAlex, USAspending, OCDS, OSV, NVD, NHTSA, eCFR,
World Bank, BLS, UN Comtrade, SAM.gov, FRED, Nexar, DigiKey, Mouser).

Two modes:

* **snapshot** (TRAIN-CANDIDATE / RAG/REFERENCE APIs): a bounded, paginated sample written as
  ``records.jsonl`` + a sanitized ``request.json``.
* **live lookup** (``LIVE-LOOKUP``: Comtrade, SAM.gov, Nexar, DigiKey, Mouser, FRED): ``lookup(query)``
  serves a cached answer while it is inside its TTL, otherwise calls the API; if the API fails the
  stale entry is *not* served (fail closed). Every cache entry is a normal staged artifact with its
  own provenance manifest, tagged ``training_allowed = False``, under
  ``rag_reference/<node>/<SRC>/live_cache/<query_key>/``.

Credentials come only from environment variables named in the registry. A missing key stops the
call before any request is made. Keys never reach disk: request descriptions omit them, response
bodies that echo them are redacted, and the manifest writer refuses any text containing one.
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from ..policy_gates import PolicyViolation
from ..provenance import assert_no_secrets, load_manifest, sha256_bytes
from .base import BaseFetcher, FetchError, FetchOutcome, MissingCredentialError

_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


class LiveLookupUnavailable(PolicyViolation):
    def __init__(self, source_id: str, reason: str) -> None:
        super().__init__("HC-06", f"{source_id}: live lookup failed and stale cache is never served ({reason})", source_id)


@dataclass
class LiveResult:
    source_id: str
    records: list
    from_cache: bool
    artifact_dir: Path
    expires_at: datetime


def dig(obj: Any, path: Optional[str]) -> Any:
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


class ApiFetcher(BaseFetcher):
    access = ("api",)
    fetch_method = "api_snapshot"

    @property
    def live(self) -> bool:
        return self.spec.intended_use == "LIVE-LOOKUP"

    def plan(self) -> dict:
        api = self.spec.api
        try:
            creds_ok = bool(self.credentials()) or not api.auth.env_vars() or api.auth.optional
        except MissingCredentialError:
            creds_ok = False
        return {**super().plan(), "mode": "live_lookup" if self.live else "snapshot", "method": api.method,
                "auth": api.auth.type, "env_vars": api.auth.env_vars(), "credentials_present": creds_ok,
                "ttl_seconds": api.ttl_seconds, "training_allowed": self.spec.training_allowed}

    def preflight(self) -> None:
        super().preflight()
        if not self.dry_run:
            self.credentials()                      # HC-06: fail closed before any request

    # -- auth ----------------------------------------------------------------------------------
    def _auth(self) -> tuple[dict, dict]:
        a, creds = self.spec.api.auth, self.credentials()
        headers, params = {}, {}
        if a.type == "header" and a.env in creds:
            headers[a.header] = creds[a.env]
        elif a.type == "query" and a.env in creds:
            params[a.param] = creds[a.env]
        elif a.type == "bearer" and a.env in creds:
            headers["Authorization"] = f"Bearer {creds[a.env]}"
        elif a.type == "oauth2_client_credentials":
            headers["Authorization"] = f"Bearer {self._oauth_token(creds)}"
        for name, ref in a.extra_headers.items():
            if ref.startswith("env:") and ref[4:] in creds:
                headers[name] = creds[ref[4:]]
        return headers, params

    def _oauth_token(self, creds: dict) -> str:
        a = self.spec.api.auth
        key = (a.token_url, creds[a.client_id_env])
        cached = _TOKEN_CACHE.get(key)
        if cached and cached[1] > time.time() + 30:
            return cached[0]
        data = {"grant_type": "client_credentials", "client_id": creds[a.client_id_env],
                "client_secret": creds[a.client_secret_env]}
        if a.scope:
            data["scope"] = a.scope
        resp = self.request("POST", a.token_url, data=data)
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise FetchError(f"{self.spec.source_id}: OAuth token endpoint returned no access_token")
        _TOKEN_CACHE[key] = (token, time.time() + float(body.get("expires_in", 600)))
        return token

    # -- calling -------------------------------------------------------------------------------
    def _request_shape(self, query: Optional[dict]) -> tuple[dict, Optional[dict]]:
        api = self.spec.api
        params, body = dict(api.params), copy.deepcopy(api.body)
        if api.graphql:
            body = {"query": api.graphql, "variables": {**api.default_query, **(query or {})}}
        elif api.method == "GET":
            params.update(api.default_query)
            params.update(query or {})
        elif query:
            body = {**(body or {}), **query}
        return params, body

    def _call(self, query: Optional[dict] = None) -> tuple[list, Optional[bytes], dict]:
        api, pag = self.spec.api, self.spec.api.pagination
        params, body = self._request_shape(query)
        auth_headers, auth_params = self._auth()
        records, raw, pages = [], None, 0
        cursor: Any = pag.start
        while True:
            p, b = dict(params), copy.deepcopy(body)
            if pag.type in ("page", "offset", "cursor"):
                target = b if (api.method == "POST" and b is not None and not api.graphql) else p
                target[pag.param] = cursor
                if pag.size_param:
                    target[pag.size_param] = pag.size
            resp = self.request(api.method, api.base_url, params={**p, **auth_params},
                                json=b if api.method == "POST" else None,
                                headers={**api.headers, **auth_headers, "Accept": "application/json, */*;q=0.5"})
            pages += 1
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype and not resp.content.lstrip()[:1] in (b"{", b"["):
                raw = resp.content
                break
            data = resp.json()
            if api.graphql and isinstance(data, dict) and data.get("errors"):
                raise FetchError(f"{self.spec.source_id}: GraphQL errors: {str(data['errors'])[:300]}")
            page_records = dig(data, api.records_path) if api.records_path else data
            page_records = page_records if isinstance(page_records, list) else ([page_records] if page_records else [])
            records += page_records
            if pag.type == "none" or pages >= pag.max_pages or not page_records:
                break
            if pag.type == "page":
                cursor = int(cursor) + 1
            elif pag.type == "offset":
                cursor = int(cursor) + pag.size
            else:
                cursor = dig(data, pag.next_path)
                if not cursor:
                    break
        description = {"method": api.method, "url": api.base_url, "params": params, "body": body, "pages": pages,
                       "auth": api.auth.type, "records_path": api.records_path}
        return records, raw, description

    def _write(self, payload: Path, records: list, raw: Optional[bytes], description: dict) -> None:
        secrets = self.secrets()
        desc = json.dumps(description, indent=2, sort_keys=True, default=str)
        assert_no_secrets(desc, secrets, "request.json")
        (payload / "request.json").write_text(desc, encoding="utf-8")
        if raw is not None:
            ext = ".xml" if raw.lstrip()[:1] == b"<" else ".txt"
            (payload / f"response{ext}").write_bytes(raw)
        else:
            with open(payload / "records.jsonl", "w", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")

    # -- snapshot ------------------------------------------------------------------------------
    def _acquire(self, payload: Path) -> dict:
        if self.live:
            return self._acquire_live(payload, None)
        records, raw, desc = self._call()
        self._write(payload, records, raw, desc)
        return {"version_tag": f"{self.spec.version_tag}-{self.now():%Y%m%d}", "fetch_method": "api_snapshot"}

    # -- live lookup with TTL cache --------------------------------------------------------------
    def query_key(self, query: Optional[dict]) -> str:
        params, body = self._request_shape(query)
        return sha256_bytes(json.dumps({"s": self.spec.source_id, "p": params, "b": body}, sort_keys=True,
                                       default=str).encode())[:16]

    def _cache_root(self, key: str) -> Path:
        return self.lake.ensure_inside(self.lake.source_dir(self.spec) / "live_cache" / key,
                                       self.lake.source_dir(self.spec))

    def _acquire_live(self, payload: Path, query: Optional[dict]) -> dict:
        records, raw, desc = self._call(query)
        self._write(payload, records, raw, desc)
        key = self.query_key(query)
        now = self.now()
        return {"version_tag": f"live-{now:%Y%m%dT%H%M%S}Z", "fetch_method": "api_live_lookup", "fetched_at": now,
                "cache_expires_at": now + timedelta(seconds=self.spec.api.ttl_seconds), "query_key": key,
                "final_parent": self._cache_root(key)}

    def cached(self, query: Optional[dict] = None) -> Optional[tuple[Path, datetime]]:
        root = self._cache_root(self.query_key(query))
        best = None
        if root.exists():
            for d in root.iterdir():
                if not (d / "provenance_manifest.json").exists():
                    continue
                m = load_manifest(d)
                if m.cache_expires_at_utc and m.cache_expires_at_utc > self.now():
                    if best is None or m.fetched_at_utc > best[2]:
                        best = (d, m.cache_expires_at_utc, m.fetched_at_utc)
        return (best[0], best[1]) if best else None

    def lookup(self, query: Optional[dict] = None) -> LiveResult:
        if not self.live:
            raise PolicyViolation("HC-06", f"{self.spec.source_id} is not a LIVE-LOOKUP source", self.spec.source_id)
        hit = self.cached(query)
        if hit is not None:
            return LiveResult(self.spec.source_id, _read_records(hit[0]), True, hit[0], hit[1])
        self.preflight()
        staging = self.lake.staging_dir(self.spec.source_id)
        try:
            payload = staging / "artifact"
            payload.mkdir()
            try:
                info = self._acquire_live(payload, query)
            except FetchError as exc:
                raise LiveLookupUnavailable(self.spec.source_id, str(exc)) from exc
            out: FetchOutcome = self._finalize(staging, info, info.get("final_parent"))
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
        if out.status not in ("STAGED", "UNCHANGED"):
            raise LiveLookupUnavailable(self.spec.source_id, "; ".join(out.reasons))
        return LiveResult(self.spec.source_id, _read_records(out.artifact_dir), False, out.artifact_dir,
                          out.manifest.cache_expires_at_utc)


def _read_records(artifact_dir: Path) -> list:
    p = Path(artifact_dir) / "artifact" / "records.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
