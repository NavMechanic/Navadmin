"""N5 supplier connectors against mocked upstream APIs (``httpx.MockTransport``): DigiKey OAuth2,
Mouser API key, Nexar GraphQL, Open Supply Hub, plus the ToS posture (feature flag, project scope,
in-memory TTL cache, per-line budget, rate limits, no raw payload persistence, secret redaction)."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.n5.config import N5Settings
from app.n5.connectors.base import ConnectorError, RateLimiter, money
from app.n5.connectors.digikey import DigiKeyConnector
from n5_support import BOURNS, MURATA, OS_ID, N5Flow, by_mpn

DK_SECRET, MOUSER_KEY, NEXAR_SECRET, OSH_TOKEN = "dk-secret-123", "mouser-key-456", "nx-secret-789", "osh-tok-000"
SENTINEL = "https://www.digikey.example/raw-only-field"


class Upstream:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.mouser_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        u = request.url
        self.calls.append(f"{request.method} {u.host}{u.path}")
        if u.host == "api.digikey.com" and u.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "dk-token", "expires_in": 600})
        if u.host == "api.digikey.com" and u.path.endswith("/productdetails"):
            mpn = u.path.split("/")[-2]
            if request.headers.get("Authorization") != "Bearer dk-token":
                return httpx.Response(401, json={"error": "unauthorized"})
            if mpn not in (MURATA, BOURNS):
                return httpx.Response(404, json={})
            price = 6.5 if mpn == MURATA else 9.25
            return httpx.Response(200, content=json.dumps({
                "Product": {"ManufacturerProductNumber": mpn, "Manufacturer": {"Name": "Murata" if mpn == MURATA
                                                                               else "Bourns"},
                            "QuantityAvailable": 9000, "ProductUrl": SENTINEL, "ProductVariations": [
                                {"DigiKeyProductNumber": f"490-{mpn}-1-ND", "PackageType": {"Name": "Cut Tape (CT)"},
                                 "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": price},
                                                     {"BreakQuantity": 100, "UnitPrice": 5.95}],
                                 "MinimumOrderQuantity": 1, "StandardPackage": 4000,
                                 "QuantityAvailableforPackageType": 8000, "MarketPlace": False},
                                {"DigiKeyProductNumber": f"490-{mpn}-6-ND", "PackageType": {"Name": "Digi-Reel®"},
                                 "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": price}],
                                 "MinimumOrderQuantity": 1, "QuantityAvailableforPackageType": 8000,
                                 "DigiReelFee": 7, "MarketPlace": False},
                                {"DigiKeyProductNumber": "MP-1", "PackageType": {"Name": "Bulk"},
                                 "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.5}], "MarketPlace": True}]},
                "SearchLocaleUsed": {"Site": "IN", "Currency": "INR"}}).encode())
        if u.host == "api.mouser.com":
            if self.mouser_status != 200:
                return httpx.Response(self.mouser_status, text=f"internal error for apiKey={MOUSER_KEY}")
            mpn = json.loads(request.content)["SearchByPartRequest"]["mouserPartNumber"]
            return httpx.Response(200, json={"Errors": [], "SearchResults": {"Parts": [
                {"ManufacturerPartNumber": mpn, "Manufacturer": "Bourns", "Min": "1", "Mult": "1",
                 "AvailabilityInStock": "1200", "LeadTime": "84 Days", "MouserPartNumber": f"652-{mpn}",
                 "PriceBreaks": [{"Quantity": 1, "Price": "₹9.10", "Currency": "INR"},
                                 {"Quantity": 10, "Price": "₹8.75", "Currency": "INR"}]},
                {"ManufacturerPartNumber": mpn + "L", "Manufacturer": "Bourns", "Min": "1", "Mult": "1",
                 "AvailabilityInStock": "50", "PriceBreaks": [{"Quantity": 1, "Price": "₹1.00", "Currency": "INR"}]}]}})
        if u.host == "identity.nexar.com":
            return httpx.Response(200, json={"access_token": "nx-token", "expires_in": 3600})
        if u.host == "api.nexar.com":
            mpn = json.loads(request.content)["variables"]["mpn"]
            return httpx.Response(200, json={"data": {"supSearchMpn": {"results": [{"part": {
                "mpn": mpn, "manufacturer": {"name": "Murata"}, "sellers": [
                    {"company": {"id": "1", "name": "SUP-DIST-B Pvt Ltd"}, "isAuthorized": True, "offers": [
                        {"sku": "B-1", "inventoryLevel": 700, "moq": 10, "orderMultiple": 10, "factoryLeadDays": 70,
                         "prices": [{"quantity": 10, "price": 6.6, "currency": "INR"}]}]},
                    {"company": {"id": "2", "name": "Bargain Components Hub"}, "isAuthorized": True, "offers": [
                        {"sku": "X-1", "inventoryLevel": 90000, "moq": 1, "orderMultiple": 1,
                         "prices": [{"quantity": 1, "price": 2.0, "currency": "INR"}]}]}]}}]}}})
        if u.host == "opensupplyhub.org":
            return httpx.Response(200, json={"id": OS_ID, "type": "Feature", "properties": {
                "os_id": OS_ID, "name": "EMS Plant 1", "address": "Plot 7", "country_code": "IN"}})
        return httpx.Response(404, json={})


def settings(**kw) -> N5Settings:
    base = dict(enable_live_apis=True, digikey_client_id=SecretStr("dk-client"),
                digikey_client_secret=SecretStr(DK_SECRET), mouser_api_key=SecretStr(MOUSER_KEY),
                nexar_client_id=SecretStr("nx-client"), nexar_client_secret=SecretStr(NEXAR_SECRET),
                osh_api_token=SecretStr(OSH_TOKEN))
    base.update(kw)
    return N5Settings(**base)


@pytest.fixture
def live(make_n1, tmp_path):
    from n3_support import n3_settings
    from app.n4.config import N4Settings

    def make(**kw):
        up = Upstream()
        c = make_n1(n3_settings=n3_settings(tmp_path / "jobs"), n4_settings=N4Settings(), n5_settings=settings(**kw),
                    n5_transport=httpx.MockTransport(up))
        flow = N5Flow(c)
        flow.bom_released()
        flow.standard_suppliers()
        for sid in ("SUP-DIGIKEY", "SUP-MOUSER"):
            flow.supplier(sid, legal_name=sid)
        return flow, flow.n5_intake(), up
    return make


def lookup(flow, intake, connectors, **kw):
    return flow.c.post(f"/v1/n5/bom-intakes/{intake['intake_id']}/lookups", "pan",
                       json={"connectors": connectors, "supplier_ids": {"DIGIKEY": "SUP-DIGIKEY",
                                                                        "MOUSER": "SUP-MOUSER"}, **kw})


def test_digikey_oauth2_lookup_creates_project_scoped_quotes(live):
    flow, intake, up = live()
    r = lookup(flow, intake, ["DIGIKEY"])
    assert r.status_code == 200, r.text
    qs = r.json()["quotes"]
    assert len(qs) == 2 and {q["channel"] for q in qs} == {"DIGIKEY_API"} and {q["source_id"] for q in qs} == \
        {"SRC-DIGIKEY"}
    m = next(q for q in qs if q["offers"][0]["quoted_mpn"] == MURATA)
    assert [o["supplier_sku"] for o in m["offers"]] == [f"490-{MURATA}-1-ND", f"490-{MURATA}-6-ND"]   # marketplace dropped
    assert m["offers"][0]["price_breaks"] == [{"min_qty": 1, "unit_price": "6.5"}, {"min_qty": 100, "unit_price": "5.95"}]
    assert m["offers"][1]["line_charges"] == [{"kind": "REELING", "amount": "7"}]
    assert m["provenance"]["connector"] == "DIGIKEY" and len(m["provenance"]["response_sha256"]) == 64
    assert m["data_use"]["training_allowed"] is False and "no bulk download" in m["provenance"]["terms"]
    assert up.calls.count("POST api.digikey.com/v1/oauth2/token") == 1


def test_mouser_prices_are_exact_and_near_matches_dropped(live):
    flow, intake, up = live()
    q = next(q for q in lookup(flow, intake, ["MOUSER"]).json()["quotes"] if q["offers"][0]["quoted_mpn"] == BOURNS)
    assert len(q["offers"]) == 1 and q["offers"][0]["price_breaks"][1] == {"min_qty": 10, "unit_price": "8.75"}
    assert q["offers"][0]["stock_qty"] == 1200
    assert money("₹6.50") == Decimal("6.50") and money("1.234,56 €") == Decimal("1234.56") and money("$0,10") == \
        Decimal("0.10")


def test_distributor_quotes_feed_the_report(live):
    flow, intake, up = live()
    lookup(flow, intake, ["DIGIKEY", "MOUSER"])
    rep = flow.n5_report(intake)
    assert rep["decision"] == "PASS"
    assert by_mpn(rep, BOURNS)["offers"][0]["supplier_id"] == "SUP-MOUSER"                # 8.75 at Q 10 beats 9.25
    assert set(rep["quote_source_ids"]) == {"SRC-DIGIKEY", "SRC-MOUSER"}


def test_nexar_sellers_map_to_supplier_records_and_unregistered_sellers_are_blocked(live):
    flow, intake, up = live()
    qs = lookup(flow, intake, ["NEXAR"], lines=[flow.L[MURATA]]).json()["quotes"]
    assert sorted(q["supplier_id"] for q in qs) == ["SUP-DIST-B", "UNREGISTERED-Bargain-Components-Hub"]
    rep = flow.n5_report(intake)
    offers = {x["supplier_id"]: x for x in by_mpn(rep, MURATA)["offers"]}
    assert offers["UNREGISTERED-Bargain-Components-Hub"]["codes"] == ["SUPPLIER_NOT_APPROVED"]
    assert offers["SUP-DIST-B"]["eligible"] and offers["SUP-DIST-B"]["q_order"] == 20


def test_lookups_are_cached_in_memory_and_raw_payloads_are_never_stored(live):
    flow, intake, up = live()
    lookup(flow, intake, ["DIGIKEY"])
    n = len(up.calls)
    second = lookup(flow, intake, ["DIGIKEY"]).json()
    assert len(up.calls) == n and all(c["cache_hit"] for c in second["calls"])
    engine = flow.c.app.state.db.engine
    with engine.connect() as conn:
        tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
        for t in tables:
            for row in conn.execute(text(f"SELECT * FROM {t}")):
                assert SENTINEL not in str(row), f"raw distributor payload persisted in {t}"
                for secret in (DK_SECRET, MOUSER_KEY, NEXAR_SECRET, OSH_TOKEN, "dk-token"):
                    assert secret not in str(row), f"secret persisted in {t}"


def test_lookups_are_scoped_to_the_active_bom(live):
    flow, intake, up = live()
    r = lookup(flow, intake, ["DIGIKEY"], lines=[99])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LOOKUP_OUT_OF_SCOPE" and not up.calls
    assert flow.c.get("/v1/n5/connector-calls", "aud").json()[-1]["outcome"] == "OUT_OF_SCOPE"


def test_per_line_budget_stops_repeated_lookups(live):
    flow, intake, up = live(cache_ttl_s=0, max_lookups_per_line=2)
    for _ in range(3):
        lookup(flow, intake, ["MOUSER"], lines=[flow.L[BOURNS]])
    outcomes = [c["outcome"] for c in flow.c.get("/v1/n5/connector-calls", "aud").json()]
    assert outcomes == ["OK", "OK", "RATE_LIMITED"]
    assert sum(1 for c in up.calls if "mouser" in c) == 2


def test_upstream_errors_are_recorded_without_secrets(live):
    flow, intake, up = live()
    up.mouser_status = 500
    r = lookup(flow, intake, ["MOUSER"])
    assert r.status_code == 200 and r.json()["quotes"] == []
    errs = [c["error"] for c in flow.c.get("/v1/n5/connector-calls", "aud").json() if c["outcome"] == "ERROR"]
    assert errs and all(MOUSER_KEY not in e and "***" in e for e in errs)


def test_unconfigured_connector_is_refused(live):
    flow, intake, up = live(mouser_api_key=None)
    r = lookup(flow, intake, ["MOUSER"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CONNECTOR_NOT_CONFIGURED"


def test_revoking_a_distributor_source_blocks_its_offers(live):
    flow, intake, up = live()
    lookup(flow, intake, ["DIGIKEY"])
    assert flow.c.post("/v1/sources/acme/SRC-DIGIKEY/approve", "rita",
                       json={"decision": "REVOKED", "reason": "API agreement ended"}).status_code == 200
    rep = flow.n5_report(intake)
    assert all(x["codes"] == ["SOURCE_RIGHTS_REVOKED"] for ln in rep["lines"] for x in ln["offers"])


def test_open_supply_hub_live_site_verification(live):
    flow, intake, up = live()
    flow.supplier("SUP-EMS-L", kind="EMS", proofs=(), approve=False,
                  sites=[{"site_id": "SITE-1", "name": "EMS Plant 1", "country": "IN", "os_id": OS_ID}])
    r = flow.c.post("/v1/n5/suppliers/SUP-EMS-L/sites/SITE-1/verify", "sq")
    assert r.status_code == 200, r.text
    ev = r.json()["sites"][0]["osh_evidence"]
    assert ev["method"] == "LIVE_API" and ev["attribution"].startswith("Open Supply Hub")
    assert f"GET opensupplyhub.org/api/facilities/{OS_ID}/" in up.calls


def test_rate_limiter_refuses_instead_of_queueing():
    t = [0.0]
    lim = RateLimiter(per_minute=2, per_day=3, clock=lambda: t[0])
    lim.acquire()
    lim.acquire()
    with pytest.raises(ConnectorError, match="per-minute"):
        lim.acquire()
    t[0] = 60.0
    lim.acquire()
    t[0] = 120.0
    with pytest.raises(ConnectorError, match="daily quota"):
        lim.acquire()


def test_digikey_parser_keeps_decimals_exact():
    offers = DigiKeyConnector.parse({"Product": {"ManufacturerProductNumber": "X", "ProductVariations": [
        {"StandardPricing": [{"BreakQuantity": 1, "UnitPrice": Decimal("0.1")}], "PackageType": {"Name": "Cut Tape"}}]}},
        "INR")
    assert offers[0]["price_breaks"][0]["unit_price"] == Decimal("0.1")


def test_every_connector_is_validated_before_any_upstream_call(live):
    flow, intake, up = live(mouser_api_key=None)
    r = lookup(flow, intake, ["DIGIKEY", "MOUSER"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CONNECTOR_NOT_CONFIGURED"
    assert up.calls == [] and flow.c.get(f"/v1/n5/bom-intakes/{intake['intake_id']}", "pan").json()["quotes"] == []
