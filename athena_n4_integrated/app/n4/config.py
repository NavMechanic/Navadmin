"""N4 settings (``N4_*`` environment variables)."""
from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import BaseModel, Field


class N4Settings(BaseModel):
    # metrology
    min_tur: float = Field(default=4.0, gt=0, description="test uncertainty ratio below this is reported (INFO)")
    max_clock_skew_s: int = Field(default=300, ge=0, description="measurements dated further in the future are refused")
    # PFMEA prioritisation: the custom heuristic unless a licensed AIAG-VDA AP table is configured
    ap_table_path: Optional[str] = Field(default=None, description="JSON lookup {S: {O: {D: H|M|L}}}, licensed")
    ap_license_ref: Optional[str] = None
    # AOI triage (model output never decides on its own)
    aoi_flag_threshold: float = Field(default=0.85, gt=0, le=1)
    aoi_ambiguous_threshold: float = Field(default=0.40, ge=0, lt=1)
    # live SCPI capture
    scpi_timeout_s: float = Field(default=10.0, gt=0)
    scpi_allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    # AI (opt-in, gated)
    llm_provider: Literal["disabled", "anthropic"] = "disabled"
    llm_model: str = "claude-opus-5-5"
    rag_top_k: int = Field(default=6, ge=1, le=20)
    # separation of duties
    require_qualification: bool = True
    max_evidence_files: int = Field(default=200, ge=1)

    @classmethod
    def from_env(cls) -> "N4Settings":
        env, kw = os.environ, {}
        for var, name in (("N4_AP_TABLE_PATH", "ap_table_path"), ("N4_AP_LICENSE_REF", "ap_license_ref"),
                          ("N4_LLM_PROVIDER", "llm_provider"), ("N4_LLM_MODEL", "llm_model")):
            if env.get(var):
                kw[name] = env[var]
        for var, name in (("N4_MIN_TUR", "min_tur"), ("N4_AOI_FLAG_THRESHOLD", "aoi_flag_threshold"),
                          ("N4_AOI_AMBIGUOUS_THRESHOLD", "aoi_ambiguous_threshold"),
                          ("N4_SCPI_TIMEOUT_S", "scpi_timeout_s")):
            if env.get(var):
                kw[name] = float(env[var])
        if env.get("N4_SCPI_ALLOWED_HOSTS"):
            kw["scpi_allowed_hosts"] = [h.strip() for h in env["N4_SCPI_ALLOWED_HOSTS"].split(",") if h.strip()]
        return cls(**kw)
