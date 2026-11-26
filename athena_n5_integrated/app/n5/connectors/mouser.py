"""Mouser Search API v1 (API key, exact part-number search).

    POST {base}/api/v1/search/partnumber?apiKey=…
         {"SearchByPartRequest": {"mouserPartNumber": "<MPN>", "partSearchOptions": "Exact"}}

The API key travels in the query string, so it is redacted from every log line and error. Prices
arrive as localised strings (``"₹6.50"``, ``"$0.10"``) and are parsed exactly into ``Decimal``.
"""
from __future__ import annotations

import re
from typing import Any

from .base import ConnectorError, HttpConnector, LookupResult, Policy, loads, money


class MouserConnector(HttpConnector):
    name = "MOUSER"
    policy = Policy(name="MOUSER", max_ttl_s=3600,
                    terms="Mouser API Terms: on-demand lookups for active project BOMs; no redistribution or "
                          "model training")

    def secrets(self) -> list[str]:
        k = self.settings.mouser_api_key
        return [k.get_secret_value()] if k else []

    def configured(self) -> bool:
        return bool(self.settings.mouser_api_key)

    def lookup(self, mpn: str) -> LookupResult:
        s = self.settings
        status, raw = self.request("POST", f"{s.mouser_base_url}/api/v1/search/partnumber",
                                   params={"apiKey": s.mouser_api_key.get_secret_value()},
                                   json={"SearchByPartRequest": {"mouserPartNumber": mpn,
                                                                 "partSearchOptions": "Exact"}})
        body = loads(raw) if status == 200 else {}
        errors = body.get("Errors") or []
        if errors:
            raise ConnectorError("UPSTREAM_ERROR", f"Mouser: {[e.get('Message') for e in errors][:3]}")
        return self.result(mpn, self.parse(body, mpn), status, raw)

    @staticmethod
    def parse(body: dict, mpn: str) -> list[dict]:
        offers = []
        for p in (body.get("SearchResults") or {}).get("Parts") or []:
            if (p.get("ManufacturerPartNumber") or "").replace(" ", "").upper() != mpn.replace(" ", "").upper():
                continue                                    # Mouser "Exact" can still return near matches
            breaks = [{"min_qty": int(b["Quantity"]), "unit_price": money(b.get("Price"))}
                      for b in p.get("PriceBreaks") or [] if money(b.get("Price"))]
            ccys = {(b.get("Currency") or "").upper() for b in p.get("PriceBreaks") or []} - {""}
            if not breaks or len(ccys) != 1:
                continue
            stock = re.sub(r"\D", "", str(p.get("AvailabilityInStock") or "")) or \
                re.sub(r"\D", "", str(p.get("Availability") or "").split(" ")[0])
            lead = re.search(r"(\d+)\s*Day", str(p.get("LeadTime") or ""), re.I)
            offers.append({"quoted_mpn": p["ManufacturerPartNumber"], "manufacturer": p.get("Manufacturer"),
                           "currency": ccys.pop(), "price_breaks": breaks, "moq": int(p.get("Min") or 1),
                           "pack_multiple": int(p.get("Mult") or 1), "stock_qty": int(stock or 0),
                           "availability": "STOCK", "factory_lead_days": int(lead.group(1)) if lead else None,
                           "supplier_sku": p.get("MouserPartNumber"), "line_charges": []})
        return offers


def mask_query(url: str) -> str:
    return re.sub(r"(apiKey=)[^&]+", r"\1***", url)


__all__ = ["MouserConnector", "mask_query", "Any"]
