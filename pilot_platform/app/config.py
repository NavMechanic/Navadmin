"""Runtime settings, read from environment variables.

Every setting has a development default so `uvicorn app.main:app` works out of
the box. In production (`PILOT_ENV=production`) the app refuses to start with
the development JWT secret or with demo users enabled.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

DEV_JWT_SECRET = "dev-only-change-me"
BASE_DIR = Path(__file__).resolve().parent.parent

# Every task routes to Claude Opus 5.5 unless LLM_ROUTES_JSON overrides it, e.g.
# {"rfq_extraction": {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low"}}
DEFAULT_ROUTE = {"provider": "local", "model": "claude-opus-5-5", "effort": "medium", "max_tokens": 16000}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    database_url: str = f"sqlite:///{BASE_DIR / 'pilot.db'}"
    jwt_secret: str = DEV_JWT_SECRET
    jwt_ttl_minutes: int = 60
    password_iterations: int = 210_000
    seed_demo: bool = True
    auto_create_schema: bool = True
    llm_routes: dict = field(default_factory=lambda: {"default": dict(DEFAULT_ROUTE)})
    default_llm_budget_usd: float = 25.0
    gst_rate: str = "0.18"
    extraction_accuracy_gate: float = 0.90
    pfmea_rpn_threshold: int = 200
    step_lease_seconds: int = 300
    retry_backoff_seconds: float = 0.0
    vendor_outbox_dir: str = str(BASE_DIR / "var" / "vendor_outbox")

    def route_for(self, task: str) -> dict:
        route = dict(DEFAULT_ROUTE)
        route.update(self.llm_routes.get("default", {}))
        route.update(self.llm_routes.get(task, {}))
        return route

    def validate(self) -> "Settings":
        if self.env == "production":
            if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < 32:
                raise RuntimeError("Set PILOT_JWT_SECRET to a random value of at least 32 characters in production")
            if self.seed_demo:
                raise RuntimeError("PILOT_SEED_DEMO must be false in production (demo users have known passwords)")
        return self

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs).validate()


def load_settings() -> Settings:
    routes = {"default": dict(DEFAULT_ROUTE)}
    routes["default"]["provider"] = os.getenv("LLM_PROVIDER", "local")
    if os.getenv("LLM_ROUTES_JSON"):
        routes.update(json.loads(os.environ["LLM_ROUTES_JSON"]))
    defaults = Settings()
    return Settings(
        env=os.getenv("PILOT_ENV", defaults.env),
        database_url=os.getenv("DATABASE_URL", defaults.database_url),
        jwt_secret=os.getenv("PILOT_JWT_SECRET", defaults.jwt_secret),
        jwt_ttl_minutes=int(os.getenv("PILOT_JWT_TTL_MINUTES", defaults.jwt_ttl_minutes)),
        password_iterations=int(os.getenv("PILOT_PASSWORD_ITERATIONS", defaults.password_iterations)),
        seed_demo=_env_bool("PILOT_SEED_DEMO", defaults.seed_demo),
        auto_create_schema=_env_bool("PILOT_AUTO_CREATE_SCHEMA", defaults.auto_create_schema),
        llm_routes=routes,
        default_llm_budget_usd=float(os.getenv("PILOT_LLM_BUDGET_USD", defaults.default_llm_budget_usd)),
        gst_rate=os.getenv("PILOT_GST_RATE", defaults.gst_rate),
        extraction_accuracy_gate=float(os.getenv("PILOT_EXTRACTION_ACCURACY_GATE", defaults.extraction_accuracy_gate)),
        pfmea_rpn_threshold=int(os.getenv("PILOT_PFMEA_RPN_THRESHOLD", defaults.pfmea_rpn_threshold)),
        step_lease_seconds=int(os.getenv("PILOT_STEP_LEASE_SECONDS", defaults.step_lease_seconds)),
        retry_backoff_seconds=float(os.getenv("PILOT_RETRY_BACKOFF_SECONDS", defaults.retry_backoff_seconds)),
        vendor_outbox_dir=os.getenv("PILOT_VENDOR_OUTBOX", defaults.vendor_outbox_dir),
    ).validate()
