"""DigiKey Product Information API v4 (OAuth2 client credentials).

    POST {base}/v1/oauth2/token                                   grant_type=client_credentials
    GET  {base}/products/v4/search/{productNumber}/productdetails X-DIGIKEY-Client-Id, Bearer token,
                                                                  X-DIGIKEY-Locale-Site / -Currency

One product per call. DigiKey's API terms prohibit bulk downloading, aggregation and building a
persistent database from API data, so N5 calls it only for MPNs of an active project BOM, caches the
response in memory for at most ``max_ttl_s`` and keeps only the decision-relevant fields (see
``base.py``). Marketplace (third-party seller) variations are dropped: they are not DigiKey stock.
"""
from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import quote

from .base import ConnectorError, HttpConnector, LookupResult, Policy, dec, loads


class DigiKeyConnector(HttpConnector):
    name = "DIGIKEY"
    policy = Policy(name="DIGIKEY", max_ttl_s=3600,
                    terms="DigiKey API Terms of Use: on-demand lookups for active project BOMs only; no bulk "
                          "download, aggregation, persistent database or model training")

    def __init__(self, settings, **kw: Any) -> None:
        super().__init__(settings, **kw)
        self._token: Optional[tuple[str, float]] = None

    def secrets(self) -> list[str]:
        s = self.settings
        return [x.get_secret_value() for x in (s.digikey_client_id, s.digikey_client_secret) if x] + \
            ([self._token[0]] if self._token else [])

    def configured(self) -> bool:
        return bool(self.settings.digikey_client_id and self.settings.digikey_client_secret)

    def token(self) -> str:
        if self._token and self._token[1] > time.monotonic() + 30:
            return self._token[0]
        s = self.settings
        status, raw = self.request("POST", f"{s.digikey_base_url}/v1/oauth2/token", data={
            "client_id": s.digikey_client_id.get_secret_value(),
            "client_secret": s.digikey_client_secret.get_secret_value(), "grant_type": "client_credentials"})
        body = loads(raw) if status == 200 else {}
        tok = body.get("access_token")
        if not tok:
            raise ConnectorError("AUTH_FAILED", "DigiKey OAuth2 token request did not return an access_token")
        self._token = (tok, time.monotonic() + float(body.get("expires_in") or 600))
        return tok

    def lookup(self, mpn: str) -> LookupResult:
        s = self.settings
        url = f"{s.digikey_base_url}/products/v4/search/{quote(mpn, safe='')}/productdetails"
        status, raw = self.request("GET", url, headers={
            "X-DIGIKEY-Client-Id": s.digikey_client_id.get_secret_value(), "Authorization": f"Bearer {self.token()}",
            "X-DIGIKEY-Locale-Site": s.digikey_locale_site, "X-DIGIKEY-Locale-Currency": s.digikey_locale_currency,
            "Accept": "application/json"})
        if status == 404:
            return self.result(mpn, [], status, raw)
        return self.result(mpn, self.parse(loads(raw), s.digikey_locale_currency), status, raw)

    @staticmethod
    def parse(body: dict, currency: str) -> list[dict]:
        prod = body.get("Product") or {}
        ccy = ((body.get("SearchLocaleUsed") or {}).get("Currency") or currency).upper()
        mpn, mfr = prod.get("ManufacturerProductNumber"), (prod.get("Manufacturer") or {}).get("Name")
        offers = []
        for v in prod.get("ProductVariations") or []:
            if v.get("MarketPlace"):
                continue                                  # third-party marketplace seller, not DigiKey stock
            breaks = [{"min_qty": int(p["BreakQuantity"]), "unit_price": dec(p["UnitPrice"])}
                      for p in v.get("StandardPricing") or [] if dec(p.get("UnitPrice"))]
            if not breaks or not mpn:
                continue
            pkg = ((v.get("PackageType") or {}).get("Name") or "").lower()
            full_reel = "tape & reel" in pkg or "(tr)" in pkg
            charges = [{"kind": "REELING", "amount": dec(v["DigiReelFee"])}] \
                if "digi-reel" in pkg and dec(v.get("DigiReelFee")) else []
            offers.append({"quoted_mpn": mpn, "manufacturer": mfr, "currency": ccy, "price_breaks": breaks,
                           "moq": int(v.get("MinimumOrderQuantity") or breaks[0]["min_qty"]),
                           "pack_multiple": int(v.get("StandardPackage") or 1) if full_reel else 1,
                           "stock_qty": int(v.get("QuantityAvailableforPackageType", prod.get("QuantityAvailable"))
                                            or 0),
                           "availability": "STOCK", "line_charges": charges,
                           "supplier_sku": v.get("DigiKeyProductNumber"), "packaging": pkg or None})
        return offers
