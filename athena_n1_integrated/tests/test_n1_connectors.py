"""N1.1 connectors: env-var auth, rate limiting, raw-payload hashing with credential redaction, the
CPPP drop zone (no scraping), CRM CSV, and import through N0. All HTTP runs on a mock transport."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr

from app.n1.config import N1Settings
from app.n1.connectors import (ComtradeConnector, ConnectorError, CpppDropZoneConnector, RateLimiter, SamGovConnector,
                               TedConnector)
from app.rules import sha256_hex
from conftest import ROOT

SAMPLES = ROOT / "examples" / "samples"


class Recorder:
    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status, self.requests = body, status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, content=self.body, headers={"content-type": "application/json"})


def test_comtrade_uses_the_subscription_key_header_or_the_public_preview():
    body = (SAMPLES / "comtrade_preview.json").read_bytes()
    rec = Recorder(body)
    keyed = ComtradeConnector(api_key="ct-secret-123", transport=httpx.MockTransport(rec))
    keyed.fetch({"reporterCode": "276", "period": "2025", "cmdCode": "8504", "flowCode": "M"})
    rec_public = Recorder(body)
    public = ComtradeConnector(transport=httpx.MockTransport(rec_public))
    f = public.fetch({"reporterCode": "276", "period": "2025", "cmdCode": "8504", "flowCode": "M"})
    assert rec.requests[0].headers["Ocp-Apim-Subscription-Key"] == "ct-secret-123"
    assert "/data/v1/get/C/A/HS" in str(rec.requests[0].url)
    assert "/public/v1/preview/C/A/HS" in str(rec_public.requests[0].url) and f.mode == "live-preview"
    assert "Ocp-Apim-Subscription-Key" not in rec_public.requests[0].headers
    obs = public.normalize(json.loads(body), datetime.now(timezone.utc)).observations[0]
    assert obs.observation_type == "TRADE_STATISTIC" and obs.buyer_country == "DE"
    assert obs.published_at.date().isoformat() == "2025-12-31" and obs.classification_codes == {"hs": ["8504"]}


def test_sam_gov_needs_a_key_and_never_stores_it(make_n1):
    with pytest.raises(ConnectorError) as exc:
        SamGovConnector().fetch({"postedFrom": "11/01/2026", "postedTo": "11/30/2026"})
    assert exc.value.code == "CONNECTOR_NOT_CONFIGURED"
    key = "sam-live-key-XYZ987"
    sample = json.loads((SAMPLES / "sam_gov_opportunities.json").read_text())
    for o in sample["opportunitiesData"]:                                       # a response that echoes the key
        o["links"] = [{"rel": "self", "href": f"https://api.sam.gov/prod/opportunities/v2/x?api_key={key}"}]
    rec = Recorder(json.dumps(sample).encode())
    n1 = make_n1(n1_settings=N1Settings(sam_gov_api_key=SecretStr(key)), connector_transport=httpx.MockTransport(rec))
    r = n1.post("/v1/n1/connectors/sam_gov/preview", "ana", json={
        "tenant_id": "acme", "mode": "live", "query": {"postedFrom": "11/01/2026", "postedTo": "11/30/2026",
                                                       "ncode": "335999"}})
    assert r.status_code == 200, r.text
    assert rec.requests[0].url.params["api_key"] == key                       # sent to SAM.gov …
    prev = r.json()
    assert "api_key" not in prev["request"]["params"]                          # … but never recorded
    store = n1.app.state.store
    stored = store.get("quarantine", store.key_for("acme", prev["raw_sha256"]))
    assert key.encode() not in stored and b"[REDACTED]" in stored
    assert sha256_hex(stored) == prev["raw_sha256"]                            # hash is of exactly what is kept
    events = n1.get("/v1/audit/events", "aud").json()
    assert key not in json.dumps(events)


def test_rate_limiter_spaces_out_live_calls():
    t = {"now": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        t["now"] += s

    lim = RateLimiter(2.0, burst=1, clock=lambda: t["now"], sleep=sleep)
    for _ in range(3):
        lim.acquire()
    assert slept == [0.5, 0.5] and lim.waited == 1.0
    rec = Recorder(b'{"notices": []}')
    ted = TedConnector(transport=httpx.MockTransport(rec), limiter=lim)
    ted.fetch({"query": "classification-cpv IN (31710000)"})
    assert len(slept) == 3


def test_ted_search_body_and_multilingual_normalization():
    rec = Recorder((SAMPLES / "ted_notices.json").read_bytes())
    ted = TedConnector(transport=httpx.MockTransport(rec))
    f = ted.fetch({"query": "classification-cpv IN (31710000)", "limit": 500})
    sent = json.loads(rec.requests[0].content)
    assert rec.requests[0].method == "POST" and sent["limit"] == 250 and "buyer-name" in sent["fields"]
    obs = {o.evidence_id: o for o in ted.normalize(json.loads(f.raw), datetime.now(timezone.utc)).observations}
    cn, can = obs["TED-000101-2026"], obs["TED-000202-2026"]
    assert cn.observation_type == "TENDER_NOTICE" and cn.buyer_name == "Stadtwerke Beispielstadt GmbH"
    assert cn.buyer_country == "DE" and str(cn.amount.amount) == "1250000" and cn.deadline_at is not None
    assert can.observation_type == "CONTRACT_AWARD" and can.amount.currency == "EUR"
    with pytest.raises(ConnectorError):
        ted.fetch({})


def test_cppp_drop_zone_orders_corrigenda_and_checks_attachments(tmp_path):
    dz = tmp_path / "dz"
    shutil.copytree(SAMPLES / "cppp_dropzone", dz)
    conn = CpppDropZoneConnector(str(dz))
    assert not hasattr(conn, "client") and conn.transport is None              # no HTTP client: no scraping
    f = conn.fetch({})
    norm = conn.normalize(json.loads(f.raw), datetime.now(timezone.utc))
    c0, c1 = norm.observations
    assert (c0.corrigendum_seq, c1.corrigendum_seq) == (0, 1) and c0.source_record_id == c1.source_record_id
    assert all(a.present for a in c0.attachments if a.required)
    assert [a.name for a in c1.attachments if a.required and not a.present] == ["Corrigendum.pdf"]
    assert any("Corrigendum.pdf" in w for w in norm.warnings)
    (dz / "2026_EXMPL_000123_1" / "NIT.pdf").write_bytes(b"swapped file")      # hash no longer matches
    c0b = conn.normalize(json.loads(conn.fetch({}).raw), datetime.now(timezone.utc)).observations[0]
    assert {a.name: a.present for a in c0b.attachments}["NIT.pdf"] is False
    with pytest.raises(ConnectorError) as exc:
        conn.fetch({"manifests": ["../../etc/passwd"]})
    assert exc.value.code == "PATH_TRAVERSAL"


def test_cppp_import_flows_through_n0_and_guardrails(make_n1, tmp_path):
    dz = tmp_path / "dz"
    shutil.copytree(SAMPLES / "cppp_dropzone", dz)
    (dz / "2026_EXMPL_000123_1" / "Corrigendum.pdf").write_bytes(b"%PDF-1.7 fictional corrigendum")
    n1 = make_n1(n1_settings=N1Settings(cppp_dropzone=str(dz)))
    n1.source("SRC-CPPP", publisher="CPP Portal export (authorized staff)", license="LicenseRef-GODL-India")
    prev = n1.post("/v1/n1/connectors/india_cppp/preview", "ana",
                   json={"tenant_id": "acme", "mode": "drop_zone"}).json()
    assert prev["mode"] == "drop_zone" and all(o["source_url"].startswith("n0://quarantine/acme/")
                                               for o in prev["observations"])
    imp = n1.post("/v1/n1/connectors/import", "ana", json={"tenant_id": "acme", "preview_id": prev["preview_id"],
                                                           "project_id": "PRJ-IN", "source_id": "SRC-CPPP"}).json()
    assert imp["n0_decision"]["decision"] == "PASS" and imp["intake"]["observations_ingested"] == 2
    rep = n1.report()
    feats = {f["evidence_id"]: f for f in rep["evidence_features"]}
    assert feats["CPPP-2026_EXMPL_000123_1-C0"]["exclusions"] == ["SUPERSEDED_CORRIGENDUM"]
    assert feats["CPPP-2026_EXMPL_000123_1-C1"]["counts_as"] == "INDICATIVE"
    assert feats["CPPP-2026_EXMPL_000123_1-C1"]["fx"]["amount_base"] == "57600.00"


def test_crm_csv_becomes_buyer_records(n1_src):
    raw = (SAMPLES / "crm_accounts.csv").read_text()
    prev = n1_src.post("/v1/n1/connectors/crm/preview", "ana", json={"tenant_id": "acme", "raw_payload": raw}).json()
    recs = {r["record_id"]: r for r in prev["buyer_records"]}
    assert recs["CRM-0001"]["buyer_identifiers"] == {"vat": "DE999999999"}
    assert recs["CRM-0002"]["stated_budget"] == {"amount": "2500000", "currency": "INR"}
    imp = n1_src.post("/v1/n1/connectors/import", "ana", json={"tenant_id": "acme", "preview_id": prev["preview_id"],
                                                               "project_id": "PRJ-CRM", "source_id": "SRC-TED"}).json()
    assert imp["n0_decision"]["decision"] == "PASS" and imp["intake"]["kind"] == "BUYER_RECORD"
    assert len(imp["intake"]["accounts"]) == 2


def test_source_errors_are_mapped_not_leaked(make_n1):
    rec = Recorder(b'{"error": "bad key ct-secret"}', status=401)
    n1 = make_n1(n1_settings=N1Settings(comtrade_api_key=SecretStr("ct-secret")),
                 connector_transport=httpx.MockTransport(rec))
    r = n1.post("/v1/n1/connectors/un_comtrade/preview", "ana", json={"tenant_id": "acme", "mode": "live",
                                                                       "query": {"cmdCode": "8504"}})
    assert r.status_code == 502 and r.json()["detail"]["code"] == "SOURCE_AUTH_FAILED"
    assert "ct-secret" not in r.text
    assert n1.post("/v1/n1/connectors/nope/preview", "ana", json={"tenant_id": "acme"}).status_code == 404
