"""Typed catalog of the 75 sources SRC-001..SRC-075, loaded from ``config/source_registry.yaml``.

The YAML is structurally validated by Pydantic, then every entry is checked against the
hard-coded guardrails in ``policy_gates`` (``check_registry_entry``). A catalog that violates
any guardrail does not load — e.g. MVTec with ``commercial_training_allowed: true``, a portal
configured as a scraper, a live lookup marked trainable, or an API key written into the YAML.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .policy_gates import (EXPECTED_SOURCE_IDS, EXPECTED_VAULT_COUNT, PolicyViolation, check_registry_entry,
                           partition_for)

DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "config" / "source_registry.yaml"
_ENV = r"^[A-Z][A-Z0-9_]{1,63}$"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthSpec(_M):
    type: Literal["none", "header", "query", "bearer", "oauth2_client_credentials"] = "none"
    header: Optional[str] = None
    param: Optional[str] = None
    env: Optional[str] = Field(default=None, pattern=_ENV)
    token_url: Optional[str] = None
    client_id_env: Optional[str] = Field(default=None, pattern=_ENV)
    client_secret_env: Optional[str] = Field(default=None, pattern=_ENV)
    scope: Optional[str] = None
    extra_headers: dict[str, str] = {}
    optional: bool = False          # key only raises rate limits (e.g. NVD, BLS); never required

    def env_vars(self) -> list[str]:
        envs = [e for e in (self.env, self.client_id_env, self.client_secret_env) if e]
        envs += [v[4:] for v in self.extra_headers.values() if v.startswith("env:")]
        return list(dict.fromkeys(envs))

    @model_validator(mode="after")
    def _shape(self):
        if self.type in ("header", "query", "bearer") and not self.env:
            raise ValueError(f"auth type {self.type} needs env")
        if self.type == "header" and not self.header or self.type == "query" and not self.param:
            raise ValueError("header/query auth needs the header/param name")
        if self.type == "oauth2_client_credentials" and not (self.token_url and self.client_id_env and self.client_secret_env):
            raise ValueError("oauth2 needs token_url, client_id_env and client_secret_env")
        return self


class PaginationSpec(_M):
    type: Literal["none", "page", "offset", "cursor"] = "none"
    param: Optional[str] = None
    size_param: Optional[str] = None
    size: int = 25
    start: int | str = 1
    next_path: Optional[str] = None
    max_pages: int = Field(default=2, ge=1, le=1000)


class ApiSpec(_M):
    base_url: str
    method: Literal["GET", "POST"] = "GET"
    auth: AuthSpec = AuthSpec()
    params: dict[str, Any] = {}
    headers: dict[str, str] = {}
    body: Optional[dict[str, Any]] = None
    graphql: Optional[str] = None
    records_path: Optional[str] = None
    pagination: PaginationSpec = PaginationSpec()
    ttl_seconds: Optional[int] = Field(default=None, ge=60)
    default_query: dict[str, Any] = {}


class GitSpec(_M):
    repo: str
    ref: str
    mode: Literal["clone", "tarball"] = "clone"
    sparse_paths: list[str] = []
    tarball_url: Optional[str] = None


class HttpFile(_M):
    url: str
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
    expected_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    extract: Literal["none", "zip", "tar"] = "none"
    expected_mime: list[str] = []


class HttpSpec(_M):
    files: list[HttpFile] = Field(min_length=1)


class DropZoneSpec(_M):
    accepted_extensions: list[str] = [".pdf"]
    entitlement_required: bool = True


class VaultSpec(_M):
    vault_name: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{2,63}$")
    schema_model: str


class TimeseriesSpec(_M):
    timestamp_column: Optional[str] = None
    source_timezone: Optional[str] = None
    value_columns: dict[str, str] = {}          # column -> unit
    group_column: Optional[str] = None
    index_kind: Literal["timestamp", "sequence"] = "timestamp"
    sequence_column: Optional[str] = None
    columns: list[str] = []                     # for headerless whitespace files (C-MAPSS)
    delimiter: Optional[str] = None             # None = comma, "whitespace"
    splits: dict[str, float] = {"train": 0.7, "val": 0.15, "test": 0.15}
    embargo: float = 0.0


class SourceSpec(_M):
    source_id: str = Field(pattern=r"^SRC-\d{3}$")
    name: str
    node_id: str = Field(pattern=r"^N[0-8]$")
    intended_use: Literal["TRAIN-CANDIDATE", "RAG/REFERENCE", "LIVE-LOOKUP", "EVAL/BENCHMARK", "PRIVATE-GROUND-TRUTH"]
    priority: Literal["P0", "P1", "P2"]
    access: Literal["git", "http_archive", "api", "drop_zone", "vault"]
    official_url: str
    license_spdx_id: str
    license_url: Optional[str] = None
    license_text: Optional[str] = None           # used when no license page/file can be snapshotted
    noncommercial: bool = False
    commercial_training_allowed: bool = False
    training_allowed: bool = False
    domain_transfer_warning: Optional[str] = None
    data_steward: str = Field(pattern=r"^steward\.[a-z0-9.\-]+$")
    version_tag: str
    origin_type: Literal["PUBLIC_OPEN", "LIVE_API", "PRIVATE_GROUND_TRUTH", "SYNTHETIC_TEST_CASE"]
    formatter: Literal["slm_jsonl", "rag_chunks", "eda_physics", "vision_yolo", "timeseries", "none"]
    record_kind: Optional[str] = None
    expected_mime: list[str] = []
    rate_limit_per_s: float = Field(default=1.0, gt=0, le=50)
    max_bytes: int = Field(default=2_000_000_000, gt=0)
    catalog_status: Literal["PROMPT_SPECIFIED", "RECONSTRUCTED", "VERIFIED"] = "RECONSTRUCTED"
    notes: str = ""
    git: Optional[GitSpec] = None
    http: Optional[HttpSpec] = None
    api: Optional[ApiSpec] = None
    drop_zone: Optional[DropZoneSpec] = None
    vault: Optional[VaultSpec] = None
    timeseries: Optional[TimeseriesSpec] = None

    @model_validator(mode="after")
    def _one_access_block(self):
        blocks = {"git": self.git, "http_archive": self.http, "api": self.api, "drop_zone": self.drop_zone,
                  "vault": self.vault}
        present = [k for k, v in blocks.items() if v is not None]
        if present != [self.access]:
            raise ValueError(f"{self.source_id}: access={self.access} needs exactly that config block (found {present})")
        return self

    @property
    def partition(self) -> str:
        return partition_for(self)

    def network_urls(self) -> list[str]:
        urls = [self.license_url] if self.license_url else []
        if self.git:
            urls += [self.git.repo] + ([self.git.tarball_url] if self.git.tarball_url else [])
        if self.http:
            urls += [f.url for f in self.http.files]
        if self.api:
            urls += [self.api.base_url] + ([self.api.auth.token_url] if self.api.auth.token_url else [])
        return urls


class RegistryError(Exception):
    def __init__(self, violations: list[PolicyViolation], structural: Optional[str] = None) -> None:
        self.violations = violations
        msg = structural or "; ".join(f"[{v.source_id}] {v}" for v in violations[:10])
        super().__init__(msg)

    @property
    def codes(self) -> set[str]:
        return {v.code for v in self.violations}


class Registry:
    def __init__(self, sources: Iterable[SourceSpec], expect_complete: bool = True) -> None:
        self._by_id: dict[str, SourceSpec] = {}
        violations: list[PolicyViolation] = []
        for s in sources:
            if s.source_id in self._by_id:
                violations.append(PolicyViolation("HC-01", f"duplicate {s.source_id}", s.source_id))
            self._by_id[s.source_id] = s
            violations += check_registry_entry(s)
        if expect_complete:
            missing = sorted(set(EXPECTED_SOURCE_IDS) - set(self._by_id))
            extra = sorted(set(self._by_id) - set(EXPECTED_SOURCE_IDS))
            if missing or extra:
                violations.append(PolicyViolation("HC-01", f"catalog must be exactly SRC-001..SRC-075 "
                                                           f"(missing {missing[:5]}, unexpected {extra[:5]})"))
            vaults = [s for s in self._by_id.values() if s.access == "vault"]
            if len(vaults) != EXPECTED_VAULT_COUNT:
                violations.append(PolicyViolation("HC-08", f"expected {EXPECTED_VAULT_COUNT} internal://athena vaults, "
                                                           f"found {len(vaults)}"))
        if violations:
            raise RegistryError(violations)

    # -- loading -------------------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict, expect_complete: bool = True) -> "Registry":
        try:
            sources = [SourceSpec.model_validate(row) for row in data.get("sources", [])]
        except ValidationError as exc:
            raise RegistryError([], structural=f"registry schema error: {exc.errors(include_url=False)[:3]}")
        return cls(sources, expect_complete=expect_complete)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_REGISTRY, expect_complete: bool = True) -> "Registry":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh), expect_complete=expect_complete)

    @staticmethod
    def raw(path: str | Path = DEFAULT_REGISTRY) -> dict:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    # -- access --------------------------------------------------------------------------------
    def get(self, source_id: str) -> SourceSpec:
        return self._by_id[source_id]

    def __iter__(self):
        return iter(sorted(self._by_id.values(), key=lambda s: s.source_id))

    def __len__(self) -> int:
        return len(self._by_id)

    def select(self, nodes: Optional[Iterable[str]] = None, priorities: Optional[Iterable[str]] = None,
               partitions: Optional[Iterable[str]] = None, source_ids: Optional[Iterable[str]] = None) -> list[SourceSpec]:
        nodes, priorities = set(nodes or []), set(priorities or [])
        partitions, ids = set(partitions or []), set(source_ids or [])
        unknown = ids - set(self._by_id)
        if unknown:
            raise KeyError(f"unknown source ids {sorted(unknown)}")
        return [s for s in self if (not nodes or s.node_id in nodes) and (not priorities or s.priority in priorities)
                and (not partitions or s.partition in partitions) and (not ids or s.source_id in ids)]
