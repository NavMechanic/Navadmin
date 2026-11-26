"""N5 settings (``N5_*`` environment variables).

Distributor API credentials are read from the environment (or injected by the platform's secret
store / egress proxy) and never stored in a record, a log line or an audit event.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, SecretStr

CONNECTORS = ("DIGIKEY", "MOUSER", "NEXAR", "OPEN_SUPPLY_HUB")


class N5Settings(BaseModel):
    # live distributor / facility APIs are off unless explicitly enabled
    enable_live_apis: bool = False
    base_currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    # ToS-scoped caching: in-memory only, per tenant + project + MPN, never persisted
    cache_ttl_s: int = Field(default=900, ge=0, le=86_400)
    max_lookups_per_line: int = Field(default=3, ge=1, description="per connector per BOM line per TTL window")
    rate_per_minute: dict[str, int] = Field(default_factory=lambda: {"DIGIKEY": 30, "MOUSER": 30, "NEXAR": 30,
                                                                     "OPEN_SUPPLY_HUB": 30})
    daily_quota: dict[str, int] = Field(default_factory=lambda: {"DIGIKEY": 1000, "MOUSER": 1000, "NEXAR": 500,
                                                                 "OPEN_SUPPLY_HUB": 1000})
    connector_sources: dict[str, str] = Field(default_factory=lambda: {
        "DIGIKEY": "SRC-DIGIKEY", "MOUSER": "SRC-MOUSER", "NEXAR": "SRC-NEXAR", "OPEN_SUPPLY_HUB": "SRC-OSH"},
        description="N0 SourceRecord that governs each connector's data (rights can be revoked there)")
    http_timeout_s: float = Field(default=15.0, gt=0)
    api_offer_validity_h: int = Field(default=24, ge=1, le=168,
                                      description="a distributor API price snapshot counts as a quote this long")
    distributor_stock_lead_days: int = Field(default=5, ge=0, description="dispatch + transit for stocked parts")
    # DigiKey (OAuth2 client credentials)
    digikey_base_url: str = "https://api.digikey.com"
    digikey_client_id: Optional[SecretStr] = None
    digikey_client_secret: Optional[SecretStr] = None
    digikey_locale_site: str = "IN"
    digikey_locale_currency: str = "INR"
    # Mouser (API key)
    mouser_base_url: str = "https://api.mouser.com"
    mouser_api_key: Optional[SecretStr] = None
    # Nexar / Octopart (OAuth2 client credentials + GraphQL)
    nexar_api_url: str = "https://api.nexar.com/graphql"
    nexar_token_url: str = "https://identity.nexar.com/connect/token"
    nexar_client_id: Optional[SecretStr] = None
    nexar_client_secret: Optional[SecretStr] = None
    # Open Supply Hub (token)
    osh_base_url: str = "https://opensupplyhub.org"
    osh_api_token: Optional[SecretStr] = None
    # risk flags and limits
    lead_time_warn_ratio: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)
    qualification_warn_days: int = Field(default=30, ge=0)
    max_quote_files: int = Field(default=50, ge=1)

    @classmethod
    def from_env(cls) -> "N5Settings":
        env, kw = os.environ, {}
        if env.get("N5_ENABLE_LIVE_APIS"):
            kw["enable_live_apis"] = env["N5_ENABLE_LIVE_APIS"].strip().lower() in ("1", "true", "yes")
        for var, name in (("N5_BASE_CURRENCY", "base_currency"), ("N5_DIGIKEY_BASE_URL", "digikey_base_url"),
                          ("N5_DIGIKEY_LOCALE_SITE", "digikey_locale_site"),
                          ("N5_DIGIKEY_LOCALE_CURRENCY", "digikey_locale_currency"),
                          ("N5_MOUSER_BASE_URL", "mouser_base_url"), ("N5_NEXAR_API_URL", "nexar_api_url"),
                          ("N5_NEXAR_TOKEN_URL", "nexar_token_url"), ("N5_OSH_BASE_URL", "osh_base_url")):
            if env.get(var):
                kw[name] = env[var]
        for var, name in (("N5_DIGIKEY_CLIENT_ID", "digikey_client_id"),
                          ("N5_DIGIKEY_CLIENT_SECRET", "digikey_client_secret"),
                          ("N5_MOUSER_API_KEY", "mouser_api_key"), ("N5_NEXAR_CLIENT_ID", "nexar_client_id"),
                          ("N5_NEXAR_CLIENT_SECRET", "nexar_client_secret"), ("N5_OSH_API_TOKEN", "osh_api_token")):
            if env.get(var):
                kw[name] = SecretStr(env[var])
        if env.get("N5_CACHE_TTL_S"):
            kw["cache_ttl_s"] = int(env["N5_CACHE_TTL_S"])
        return cls(**kw)
