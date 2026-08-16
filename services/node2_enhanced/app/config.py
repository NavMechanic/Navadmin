"""Configuration for Node 2.

Every setting is overridable by environment variable (prefix ``ATHENA_``) or a
``.env`` file. Nothing here needs a value for the service to start: without a
PostgreSQL URL it falls back to a local SQLite file, and without Compliance
Vault credentials it serves deterministic replay fixtures. Both fallbacks are
reported by ``/health`` rather than assumed.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="ATHENA_", extra="ignore", case_sensitive=False
    )

    # ---------------------------------------------------------- service
    app_name: str = "Athena Node 2 — Micro-PLM & Stage-Gate Governance"
    version: str = "1.0.0"
    environment: Literal["dev", "test", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ---------------------------------------------------------- database
    # Async driver required. PostgreSQL is the target; SQLite is the escape
    # hatch so the test suite and a laptop demo need no server.
    database_url: str = "postgresql+asyncpg://athena:athena@localhost:5432/athena_plm"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800

    seed_path: Path = DATA_DIR / "node2_seed.json"

    # ---------------------------------------------------------- auth
    # RS256 would be the production choice with the key served from the IdP's
    # JWKS endpoint. HS256 with a shared secret keeps the demo self-contained;
    # the algorithm and audience are both configurable so swapping is a config
    # change, not a code change.
    jwt_secret: str = "athena-node2-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "athena.plm"
    jwt_audience: str = "athena.node2"
    jwt_ttl_minutes: int = 480
    auth_required: bool = True

    # ---------------------------------------------------------- gate policy
    # IEC 62368-1 creepage depends on working voltage, pollution degree and
    # material group. 3.0 mm is the programme's stated minimum for the 20 W
    # adapter; it is configurable precisely because the real number must come
    # from the safety assessment, not from a constant in a service.
    creepage_min_mm_adapter: float = 3.0
    creepage_guard_band_pct: float = 20.0
    fmea_high_ap_requires_named_owner: bool = True
    # A parent cannot pass a gate its children have not passed. Turning this
    # off is allowed but is recorded in the evaluation payload.
    enforce_child_gate_precedence: bool = True
    # Certificates expiring inside this window fail rather than pass, because a
    # certificate that dies mid-ramp is not a certificate you have.
    certificate_min_validity_days: int = 90

    # ---------------------------------------------------------- vaults
    cad_vault_webhook_secret: str = "cad-vault-dev-secret"
    cad_vault_base_url: str = ""
    compliance_vault_base_url: str = ""
    compliance_vault_api_key: str = ""
    http_timeout_seconds: float = 15.0
    webhook_max_skew_seconds: int = 300

    @field_validator("database_url")
    @classmethod
    def _must_be_async(cls, v: str) -> str:
        """A sync driver here deadlocks the event loop under load.

        Failing at startup with a clear message beats a service that works in
        development and stalls the first time two requests overlap.
        """
        ok = ("+asyncpg", "+aiosqlite", "+asyncmy", "+aiomysql", "+psycopg_async")
        if not any(marker in v for marker in ok):
            raise ValueError(
                f"ATHENA_DATABASE_URL must use an async driver (one of {', '.join(ok)}). "
                f"Got {v.split('://')[0]!r}. For PostgreSQL use "
                "postgresql+asyncpg://user:pass@host:5432/dbname"
            )
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def compliance_offline(self) -> bool:
        """True when no Compliance Vault is configured — replay fixtures."""
        return not self.compliance_vault_base_url

    @property
    def cad_vault_offline(self) -> bool:
        return not self.cad_vault_base_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
