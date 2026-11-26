"""N0 service settings (environment-driven; tests pass a ``Settings`` directly).

Environment variables (all optional):
  N0_DATABASE_URL        postgresql+psycopg://user:pass@host/db  (default: SQLite under ./var)
  N0_STORAGE_BACKEND     local | s3              N0_STORAGE_ROOT / N0_S3_BUCKET / N0_S3_PREFIX
  N0_TOKENS_JSON         {"<token>": {"principal_id": ..., "tenant_id": ..., "roles": [...], ...}}
  N0_MAX_UPLOAD_BYTES    default 52428800 (50 MiB)
  N0_CLAMD_HOST/PORT     use a ClamAV daemon for malware scanning (else built-in signature scanner)
  N0_REQUIRE_AV          true → uploads are rejected unless a real AV engine is configured
  N0_LLM_PROVIDER        disabled (default) | deterministic | anthropic
  N0_LLM_MODEL           default claude-opus-5-5          N0_LLM_EFFORT  low|medium|high (default medium)
  N0_AUTO_CREATE_SCHEMA  true (dev/test) — production runs `alembic upgrade head` instead
  N0_JWT_SECRET          HS256 shared secret, or N0_JWT_PUBLIC_KEY / N0_JWT_PUBLIC_KEY_FILE (RS256/ES256 PEM)
  N0_JWT_ISSUER / N0_JWT_AUDIENCE / N0_JWT_ALGORITHMS   optional JWT validation settings
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Settings(BaseModel):
    database_url: str = "sqlite:///./var/athena_n0.db"
    storage_backend: Literal["local", "s3"] = "local"
    storage_root: str = "./var/object_store"
    s3_bucket: Optional[str] = None
    s3_prefix: str = "athena-n0"
    tokens: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_upload_bytes: int = 50 * 1024 * 1024
    clamd_host: Optional[str] = None
    clamd_port: int = 3310
    require_av: bool = False
    llm_provider: Literal["disabled", "deterministic", "anthropic"] = "disabled"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high"] = "medium"
    auto_create_schema: bool = True
    jwt_secret: Optional[str] = None
    jwt_public_key: Optional[str] = None
    jwt_algorithms: list[str] = Field(default_factory=list)
    jwt_issuer: Optional[str] = None
    jwt_audience: Optional[str] = None
    extra_roles: list[str] = Field(default_factory=list)
    role_implications: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        kw: dict[str, Any] = {}
        mapping = {"N0_DATABASE_URL": "database_url", "N0_STORAGE_BACKEND": "storage_backend",
                   "N0_STORAGE_ROOT": "storage_root", "N0_S3_BUCKET": "s3_bucket", "N0_S3_PREFIX": "s3_prefix",
                   "N0_CLAMD_HOST": "clamd_host", "N0_LLM_PROVIDER": "llm_provider", "N0_LLM_MODEL": "llm_model",
                   "N0_LLM_EFFORT": "llm_effort", "N0_JWT_SECRET": "jwt_secret", "N0_JWT_ISSUER": "jwt_issuer",
                   "N0_JWT_AUDIENCE": "jwt_audience"}
        for var, name in mapping.items():
            if env.get(var):
                kw[name] = env[var]
        if env.get("N0_JWT_PUBLIC_KEY_FILE"):
            with open(env["N0_JWT_PUBLIC_KEY_FILE"], encoding="utf-8") as fh:
                kw["jwt_public_key"] = fh.read()
        elif env.get("N0_JWT_PUBLIC_KEY"):
            kw["jwt_public_key"] = env["N0_JWT_PUBLIC_KEY"]
        if env.get("N0_JWT_ALGORITHMS"):
            kw["jwt_algorithms"] = [a.strip() for a in env["N0_JWT_ALGORITHMS"].split(",") if a.strip()]
        for var, name in (("N0_MAX_UPLOAD_BYTES", "max_upload_bytes"), ("N0_CLAMD_PORT", "clamd_port")):
            if env.get(var):
                kw[name] = int(env[var])
        for var, name in (("N0_REQUIRE_AV", "require_av"), ("N0_AUTO_CREATE_SCHEMA", "auto_create_schema")):
            if env.get(var):
                kw[name] = env[var].lower() in ("1", "true", "yes")
        if env.get("N0_TOKENS_JSON"):
            kw["tokens"] = json.loads(env["N0_TOKENS_JSON"])
        return cls(**kw)
