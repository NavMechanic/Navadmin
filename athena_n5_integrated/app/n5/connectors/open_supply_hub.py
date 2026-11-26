"""Open Supply Hub: facility identification for EMS / ODM / factory sites (REST or offline CSV).

    GET {base}/api/facilities/{os_id}/        Authorization: Token <token>   → GeoJSON Feature
    offline: an OSH facility CSV export with at least os_id, name, address, country_code

A site is marked ``osh_verified`` only when the OS ID resolves and its country matches the
supplier's declared site. The result is evidence for the supplier-quality reviewer, never an
approval. OSH data is used with the attribution its terms require.
"""
from __future__ import annotations

import csv
import io
from typing import Any, Optional
from urllib.parse import quote

from .base import HttpConnector, LookupResult, Policy, loads


class OpenSupplyHubConnector(HttpConnector):
    name = "OPEN_SUPPLY_HUB"
    policy = Policy(name="OPEN_SUPPLY_HUB", max_ttl_s=86_400,
                    terms="Open Supply Hub terms of use: open facility data with attribution")

    def secrets(self) -> list[str]:
        t = self.settings.osh_api_token
        return [t.get_secret_value()] if t else []

    def configured(self) -> bool:
        return bool(self.settings.osh_api_token)

    def lookup(self, os_id: str) -> LookupResult:
        s = self.settings
        status, raw = self.request("GET", f"{s.osh_base_url}/api/facilities/{quote(os_id, safe='')}/",
                                   headers={"Authorization": f"Token {s.osh_api_token.get_secret_value()}",
                                            "Accept": "application/json"})
        if status == 404:
            return self.result(os_id, [], status, raw)
        fac = self.parse_feature(loads(raw))
        return self.result(os_id, [fac] if fac else [], status, raw)

    @staticmethod
    def parse_feature(body: dict) -> Optional[dict[str, Any]]:
        props = body.get("properties") or {}
        os_id = props.get("os_id") or body.get("id")
        if not os_id:
            return None
        return {"os_id": os_id, "name": props.get("name"), "address": props.get("address"),
                "country_code": (props.get("country_code") or "").upper() or None,
                "attribution": "Open Supply Hub (opensupplyhub.org)"}

    @staticmethod
    def parse_csv(data: bytes) -> dict[str, dict[str, Any]]:
        text = data.decode("utf-8-sig", errors="replace")
        rows = csv.DictReader(io.StringIO(text))
        out = {}
        for r in rows:
            os_id = (r.get("os_id") or "").strip()
            if os_id:
                out[os_id] = {"os_id": os_id, "name": (r.get("name") or "").strip() or None,
                              "address": (r.get("address") or "").strip() or None,
                              "country_code": (r.get("country_code") or "").strip().upper() or None,
                              "attribution": "Open Supply Hub (opensupplyhub.org) CSV export"}
        return out
