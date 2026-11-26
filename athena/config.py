"""Athena orchestrator configuration — loaded from env vars with safe defaults."""

from __future__ import annotations

import os


class AthenaConfig:
    # Pecan AI
    PECAN_API_KEY: str = os.getenv("PECAN_API_KEY", "")
    PECAN_BASE_URL: str = os.getenv("PECAN_BASE_URL", "https://api.pecan.ai/v1")
    PECAN_MIN_PROBABILITY: float = float(os.getenv("PECAN_MIN_PROBABILITY", "0.75"))

    # Duro PLM
    DURO_API_KEY: str = os.getenv("DURO_API_KEY", "")
    DURO_BASE_URL: str = os.getenv("DURO_BASE_URL", "https://app.duro.io/api/v1")
    DURO_BOM_REVISION: str = os.getenv("DURO_BOM_REVISION", "latest")

    # Nexar
    NEXAR_CLIENT_ID: str = os.getenv("NEXAR_CLIENT_ID", "")
    NEXAR_CLIENT_SECRET: str = os.getenv("NEXAR_CLIENT_SECRET", "")
    NEXAR_BASE_URL: str = os.getenv("NEXAR_BASE_URL", "https://api.nexar.com/graphql")

    # Lokad
    LOKAD_TOKEN: str = os.getenv("LOKAD_TOKEN", "")
    LOKAD_BASE_URL: str = os.getenv("LOKAD_BASE_URL", "https://api.lokad.com/rest/v1")
    LOKAD_ACCOUNT_ID: str = os.getenv("LOKAD_ACCOUNT_ID", "")

    # Ansys Twin Builder / Gateway
    ANSYS_TWIN_URL: str = os.getenv("ANSYS_TWIN_URL", "")
    ANSYS_API_KEY: str = os.getenv("ANSYS_API_KEY", "")

    # MacroFab
    MACROFAB_API_KEY: str = os.getenv("MACROFAB_API_KEY", "")
    MACROFAB_BASE_URL: str = os.getenv("MACROFAB_BASE_URL", "https://macrofab.com/api/v1")

    # Fictiv
    FICTIV_CLIENT_ID: str = os.getenv("FICTIV_CLIENT_ID", "")
    FICTIV_CLIENT_SECRET: str = os.getenv("FICTIV_CLIENT_SECRET", "")
    FICTIV_BASE_URL: str = os.getenv("FICTIV_BASE_URL", "https://api.fictiv.com/v2")

    # Instrumental AI
    INSTRUMENTAL_API_KEY: str = os.getenv("INSTRUMENTAL_API_KEY", "")
    INSTRUMENTAL_BASE_URL: str = os.getenv("INSTRUMENTAL_BASE_URL", "https://api.instrumental.com/v1")
    INSTRUMENTAL_STREAM_URL: str = os.getenv("INSTRUMENTAL_STREAM_URL", "wss://stream.instrumental.com/vision")

    # AWS IoT TwinMaker
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    TWINMAKER_WORKSPACE_ID: str = os.getenv("TWINMAKER_WORKSPACE_ID", "athena-workspace")
    TWINMAKER_ASSET_MODEL_ID: str = os.getenv("TWINMAKER_ASSET_MODEL_ID", "athena-device")

    # RaaS / YaSS Revenue Engine
    RAAS_BASE_MONTHLY_USD: float = float(os.getenv("RAAS_BASE_MONTHLY_USD", "150.0"))
    YASS_BASE_ANNUAL_USD: float = float(os.getenv("YASS_BASE_ANNUAL_USD", "1200.0"))
    REVENUE_QUALITY_MULTIPLIER: float = float(os.getenv("REVENUE_QUALITY_MULTIPLIER", "1.5"))

    # Generative Design (Fusion 360)
    FUSION_TOKEN: str = os.getenv("FUSION_TOKEN", "")

    # Automated Testing (Pluma)
    PLUMA_TOKEN: str = os.getenv("PLUMA_TOKEN", "")

    # Orchestrator
    ATHENA_DRY_RUN: bool = os.getenv("ATHENA_DRY_RUN", "true").lower() in ("1", "true", "yes")
    HTTP_TIMEOUT_S: float = float(os.getenv("ATHENA_HTTP_TIMEOUT_S", "30.0"))


cfg = AthenaConfig()
