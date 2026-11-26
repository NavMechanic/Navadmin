"""N5 supplier connectors: DigiKey (OAuth2), Mouser (API key), Nexar / Octopart (GraphQL), Open Supply
Hub (REST / CSV) and the EMS / ODM signed drop zone. All live APIs are off unless
``N5_ENABLE_LIVE_APIS=true``; see ``base.py`` for the licensing posture."""
from __future__ import annotations

from typing import Optional

import httpx

from .base import ConnectorError, HttpConnector, LookupResult, RateLimiter, TTLCache
from .digikey import DigiKeyConnector
from .mouser import MouserConnector
from .nexar import NexarConnector
from .open_supply_hub import OpenSupplyHubConnector

CLASSES = {"DIGIKEY": DigiKeyConnector, "MOUSER": MouserConnector, "NEXAR": NexarConnector,
           "OPEN_SUPPLY_HUB": OpenSupplyHubConnector}


def make_connectors(settings, transport: Optional[httpx.BaseTransport] = None) -> dict[str, HttpConnector]:
    return {name: cls(settings, transport=transport) for name, cls in CLASSES.items()}


__all__ = ["CLASSES", "ConnectorError", "HttpConnector", "LookupResult", "RateLimiter", "TTLCache",
           "make_connectors"]
