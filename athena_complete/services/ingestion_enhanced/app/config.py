"""Configuration for the ingestion and parsing gateway."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="ATHENA_", extra="ignore", case_sensitive=False)

    app_name: str = "Athena Ingestion & Parsing Gateway"
    version: str = "1.0.0"
    environment: Literal["dev", "test", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    api_key: str = ""

    database_url: str = (
        "postgresql+asyncpg://athena:athena@127.0.0.1:5432/athena_ingest")
    db_echo: bool = False
    db_pool_size: int = 10

    # ---------------------------------------------------------------- policy
    # The setting that defines this service. With it true, a parameter that
    # could not be extracted is reported missing and never defaulted. Turning
    # it off is possible and is loudly reported in /health, because a defaulted
    # safety parameter is indistinguishable from a measured one downstream.
    refuse_defaults: bool = True
    # A document whose safety-relevant parameters are all missing is held for
    # review rather than published to the platform.
    hold_incomplete_safety: bool = True
    max_upload_mb: int = 40

    # ---------------------------------------------------------------- workers
    celery_broker_url: str = "redis://127.0.0.1:6379/0"
    celery_result_backend: str = "redis://127.0.0.1:6379/1"
    dispatch_async: bool = True
    mlflow_tracking_uri: str = ""

    @model_validator(mode="after")
    def _prod_guards(self) -> "Settings":
        if self.environment == "prod":
            if not self.api_key:
                raise ValueError(
                    "ATHENA_API_KEY must be set when ATHENA_ENVIRONMENT=prod.")
            if not self.refuse_defaults:
                raise ValueError(
                    "ATHENA_REFUSE_DEFAULTS=false is refused in production. A "
                    "defaulted thermal resistance or creepage distance becomes "
                    "indistinguishable from a measured one the moment it is "
                    "stored, and every downstream calculation inherits it.")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
