"""Nexar (Octopart) Supply GraphQL API (OAuth2 client credentials, scope ``supply.domain``).

One MPN per query. Only sellers Nexar marks as authorized are requested (``authorizedOnly: true``);
each seller becomes its own quote, mapped to an N5 supplier record by company name. A seller with
no approved N5 supplier record is still shown, and is then rejected by the guardrails
(``SUPPLIER_NOT_APPROVED``): Nexar's flag is evidence for supplier quality, never an approval.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .base import ConnectorError, HttpConnector, LookupResult, Policy, dec, loads

QUERY = """query N5Offers($mpn: String!) {
  supSearchMpn(q: $mpn, limit: 3) {
    results { part { mpn manufacturer { name }
      sellers(authorizedOnly: true) { company { id name } isAuthorized
        offers { sku inventoryLevel moq orderMultiple packaging factoryLeadDays
                 prices { quantity price currency } } } } } } }"""


class NexarConnector(HttpConnector):
    name = "NEXAR"
    policy = Policy(name="NEXAR", max_ttl_s=3600,
                    terms="Nexar API Terms: on-demand part lookups within the licensed part quota; no resale, "
                          "redistribution or model training")

    def __init__(self, settings, **kw: Any) -> None:
        super().__init__(settings, **kw)
        self._token: Optional[tuple[str, float]] = None

    def secrets(self) -> list[str]:
        s = self.settings
        return [x.get_secret_value() for x in (s.nexar_client_id, s.nexar_client_secret) if x] + \
            ([self._token[0]] if self._token else [])

    def configured(self) -> bool:
        return bool(self.settings.nexar_client_id and self.settings.nexar_client_secret)

    def token(self) -> str:
        if self._token and self._token[1] > time.monotonic() + 30:
            return self._token[0]
        s = self.settings
        status, raw = self.request("POST", s.nexar_token_url, data={
            "grant_type": "client_credentials", "client_id": s.nexar_client_id.get_secret_value(),
            "client_secret": s.nexar_client_secret.get_secret_value(), "scope": "supply.domain"})
        body = loads(raw) if status == 200 else {}
        if not body.get("access_token"):
            raise ConnectorError("AUTH_FAILED", "Nexar token request did not return an access_token")
        self._token = (body["access_token"], time.monotonic() + float(body.get("expires_in") or 3600))
        return self._token[0]

    def lookup(self, mpn: str) -> LookupResult:
        status, raw = self.request("POST", self.settings.nexar_api_url, json={"query": QUERY,
                                                                              "variables": {"mpn": mpn}},
                                   headers={"Authorization": f"Bearer {self.token()}"})
        body = loads(raw) if status == 200 else {}
        if body.get("errors"):
            raise ConnectorError("UPSTREAM_ERROR", f"Nexar GraphQL: {[e.get('message') for e in body['errors']][:3]}")
        return self.result(mpn, self.parse(body, mpn), status, raw)

    @staticmethod
    def parse(body: dict, mpn: str) -> list[dict]:
        out = []
        for r in ((body.get("data") or {}).get("supSearchMpn") or {}).get("results") or []:
            part = r.get("part") or {}
            if (part.get("mpn") or "").replace(" ", "").upper() != mpn.replace(" ", "").upper():
                continue
            for seller in part.get("sellers") or []:
                if not seller.get("isAuthorized"):
                    continue
                company = (seller.get("company") or {}).get("name")
                for o in seller.get("offers") or []:
                    prices = [p for p in o.get("prices") or [] if dec(p.get("price"))]
                    ccys = {(p.get("currency") or "").upper() for p in prices} - {""}
                    for ccy in sorted(ccys):
                        breaks = [{"min_qty": int(p["quantity"]), "unit_price": dec(p["price"])}
                                  for p in prices if (p.get("currency") or "").upper() == ccy]
                        out.append({"quoted_mpn": part["mpn"], "manufacturer": (part.get("manufacturer") or {})
                                    .get("name"), "seller": company, "currency": ccy, "price_breaks": breaks,
                                    "moq": int(o.get("moq") or breaks[0]["min_qty"]),
                                    "pack_multiple": int(o.get("orderMultiple") or 1),
                                    "stock_qty": max(int(o.get("inventoryLevel") or 0), 0), "availability": "STOCK",
                                    "factory_lead_days": o.get("factoryLeadDays"), "supplier_sku": o.get("sku"),
                                    "line_charges": []})
        return out
