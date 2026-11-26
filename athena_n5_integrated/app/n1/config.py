"""N1 settings (environment-driven; tests construct ``N1Settings`` directly).

  COMTRADE_API_KEY       UN Comtrade subscription key (without it only the public preview endpoint is used)
  SAM_GOV_API_KEY        SAM.gov public API key (required for live SAM.gov calls)
  TED_API_KEY            optional; TED search is anonymous
  N1_CPPP_DROPZONE       directory where authorized staff drop India CPPP tender exports (no scraping)
  N1_RATE_<CONNECTOR>    requests per second per connector, e.g. N1_RATE_SAM_GOV=2
  N1_SLM_PROVIDER        disabled (default) | rules | anthropic      N1_SLM_MODEL (default claude-haiku-5-5)
  N1_SLM_GOLD_PATH       held-out gold set (JSONL) the SLM must pass before extraction is enabled
  N1_EXPLAINER_PROVIDER  disabled (default) | template | anthropic   N1_EXPLAINER_MODEL (default claude-opus-5-5)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, SecretStr

BUNDLED_GOLD = Path(__file__).resolve().parent / "gold" / "tender_extraction_heldout.jsonl"


class N1Settings(BaseModel):
    comtrade_api_key: Optional[SecretStr] = None
    sam_gov_api_key: Optional[SecretStr] = None
    ted_api_key: Optional[SecretStr] = None
    cppp_dropzone: Optional[str] = None
    rate_limits: dict[str, float] = Field(default_factory=lambda: {"un_comtrade": 1.0, "sam_gov": 2.0, "ted_eu": 2.0})
    http_timeout_s: float = 30.0
    slm_provider: Literal["disabled", "rules", "anthropic"] = "disabled"
    slm_model: str = "claude-haiku-5-5"
    slm_gold_path: str = str(BUNDLED_GOLD)
    explainer_provider: Literal["disabled", "template", "anthropic"] = "disabled"
    explainer_model: str = "claude-opus-5-5"

    @classmethod
    def from_env(cls) -> "N1Settings":
        env, kw = os.environ, {}
        for var, name in (("COMTRADE_API_KEY", "comtrade_api_key"), ("SAM_GOV_API_KEY", "sam_gov_api_key"),
                          ("TED_API_KEY", "ted_api_key"), ("N1_CPPP_DROPZONE", "cppp_dropzone"),
                          ("N1_SLM_PROVIDER", "slm_provider"), ("N1_SLM_MODEL", "slm_model"),
                          ("N1_SLM_GOLD_PATH", "slm_gold_path"), ("N1_EXPLAINER_PROVIDER", "explainer_provider"),
                          ("N1_EXPLAINER_MODEL", "explainer_model")):
            if env.get(var):
                kw[name] = env[var]
        rates = cls().rate_limits
        for name in list(rates):
            v = env.get(f"N1_RATE_{name.upper()}")
            if v:
                rates[name] = float(v)
        kw["rate_limits"] = rates
        return cls(**kw)
