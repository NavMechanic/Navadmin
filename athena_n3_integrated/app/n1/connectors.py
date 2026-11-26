"""N1.1 market-data source adapters.

Every adapter turns a raw source response into ``MarketObservation`` (or ``BuyerRecord``) objects. It
never ingests anything itself: previews are staged, then imported as an N0 artifact, and N1 only
reads what N0 passed.

* ``UN_COMTRADE``  trade flows by HS code (sector context, a TAM proxy). Uses the authenticated
  ``data/v1/get`` API when ``COMTRADE_API_KEY`` is set, otherwise the public ``public/v1/preview`` API.
* ``SAM_GOV``      US federal opportunities and awards, Get Opportunities Public API v2 (``SAM_GOV_API_KEY``).
* ``TED_EU``       EU notices and awards, TED Search API v3 (anonymous search).
* ``INDIA_CPPP``   controlled drop zone only: authorized staff export tenders from the CPP Portal
  and place a JSON manifest plus attachments in ``N1_CPPP_DROPZONE``. The adapter contains no HTTP
  client; it orders corrigenda and checks the mandatory attachments against their SHA-256.
* ``CRM``          structured CRM / interview exports (JSON or CSV) → ``BuyerRecord``.

Live calls are rate-limited (token bucket per connector, ``N1_RATE_*``) and use an injectable httpx
transport, so tests run offline. Credentials are removed from the raw payload before it is hashed
and stored. ``raw_sha256`` is the hash of exactly the bytes N1 keeps.

The field mappings follow the documented response shapes of these APIs, but they have only been
exercised against offline samples here. Verify them against the live APIs before production use.
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

import httpx

from ..errors import N0Error
from ..models import BuyerRecord, MarketObservation, TenderAttachment
from ..rules import sha256_hex

ISO3_TO_ISO2 = {"AUT": "AT", "BEL": "BE", "BGR": "BG", "HRV": "HR", "CYP": "CY", "CZE": "CZ", "DNK": "DK",
                "EST": "EE", "FIN": "FI", "FRA": "FR", "DEU": "DE", "GRC": "GR", "HUN": "HU", "IRL": "IE",
                "ITA": "IT", "LVA": "LV", "LTU": "LT", "LUX": "LU", "MLT": "MT", "NLD": "NL", "POL": "PL",
                "PRT": "PT", "ROU": "RO", "SVK": "SK", "SVN": "SI", "ESP": "ES", "SWE": "SE", "NOR": "NO",
                "CHE": "CH", "ISL": "IS", "GBR": "GB", "USA": "US", "IND": "IN", "CHN": "CN", "JPN": "JP",
                "KOR": "KR", "CAN": "CA", "MEX": "MX", "BRA": "BR", "AUS": "AU"}


class ConnectorError(N0Error):
    pass


class RateLimiter:
    """Token bucket: ``rate`` requests per second, bursts up to ``burst``. Clock and sleep are injectable."""

    def __init__(self, rate: float, burst: int = 1, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate, self.capacity, self.clock, self.sleep = rate, float(burst), clock, sleep
        self.tokens, self.updated = float(burst), clock()
        self.waited = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                self.sleep(wait)
                self.waited += wait
                self.updated = self.clock()
                self.tokens = 0.0
            else:
                self.tokens -= 1


@dataclass
class RawFetch:
    raw: bytes
    request: dict[str, Any]
    mode: str


@dataclass
class Normalized:
    observations: list[MarketObservation] = field(default_factory=list)
    buyer_records: list[BuyerRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    record_count: int = 0


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v).replace(",", ""))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _dt(v: Any) -> Optional[datetime]:
    """Parse source dates: ISO datetimes with offsets, plain dates, TED's '2026-11-20+01:00', SAM's MM/dd/yyyy."""
    if not v:
        return None
    s = str(v).strip()
    try:
        if len(s) >= 10 and s[2] == "/" and s[5] == "/":
            return datetime.strptime(s[:10], "%m/%d/%Y").replace(tzinfo=timezone.utc)
        if len(s) > 10 and s[10] in "+-Z" and "T" not in s:
            s = s[:10]                                    # date with zone suffix → date
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _first(v: Any) -> Any:
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _ml(v: Any, prefer: tuple[str, ...] = ("eng", "en", "ENG")) -> Optional[str]:
    """TED multilingual values: {'eng': 'x'} or {'deu': ['y']} → a string."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, list):
        return _ml(_first(v), prefer)
    if isinstance(v, dict):
        for lang in prefer:
            if lang in v:
                return _ml(v[lang], prefer)
        for val in v.values():
            return _ml(val, prefer)
    return str(v)


class Connector:
    name = "abstract"
    source_connector = "MANUAL"
    key_env: Optional[str] = None

    def __init__(self, *, api_key: Optional[str] = None, transport: Optional[httpx.BaseTransport] = None,
                 limiter: Optional[RateLimiter] = None, timeout: float = 30.0) -> None:
        self.api_key, self.transport, self.timeout = api_key, transport, timeout
        self.limiter = limiter or RateLimiter(1.0)

    # ---------------------------------------------------------------- live
    def fetch(self, query: dict[str, Any]) -> RawFetch:  # pragma: no cover - interface
        raise ConnectorError(400, "LIVE_NOT_SUPPORTED", f"{self.name} has no live mode")

    def _http(self, method: str, url: str, *, params=None, json_body=None, headers=None) -> bytes:
        self.limiter.acquire()
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                r = client.request(method, url, params=params, json=json_body, headers=headers or {})
        except httpx.HTTPError as exc:
            raise ConnectorError(502, "SOURCE_UNREACHABLE", f"{self.name}: {type(exc).__name__}") from exc
        if r.status_code == 429:
            raise ConnectorError(429, "SOURCE_RATE_LIMITED", f"{self.name} rate-limited the request")
        if r.status_code in (401, 403):
            raise ConnectorError(502, "SOURCE_AUTH_FAILED", f"{self.name} rejected the credentials "
                                                            f"(HTTP {r.status_code})")
        if r.status_code >= 400:
            raise ConnectorError(502, "SOURCE_ERROR", f"{self.name} returned HTTP {r.status_code}")
        return r.content

    def secrets(self) -> list[str]:
        return [self.api_key] if self.api_key else []

    # ---------------------------------------------------------------- normalize
    def parse_raw(self, raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConnectorError(422, "RAW_NOT_JSON", f"{self.name} payload is not JSON: {exc}") from exc

    def normalize(self, obj: Any, retrieved_at: datetime) -> Normalized:  # pragma: no cover - interface
        raise NotImplementedError


def redact(raw: bytes, secrets: list[str]) -> bytes:
    for s in secrets:
        if s:
            raw = raw.replace(s.encode(), b"[REDACTED]")
    return raw


# ----------------------------------------------------------------------------------------------
class ComtradeConnector(Connector):
    name, source_connector, key_env = "un_comtrade", "UN_COMTRADE", "COMTRADE_API_KEY"
    DATA_URL = "https://comtradeapi.un.org/data/v1/get/C/{freq}/HS"
    PREVIEW_URL = "https://comtradeapi.un.org/public/v1/preview/C/{freq}/HS"
    PARAMS = ("reporterCode", "partnerCode", "period", "cmdCode", "flowCode", "partner2Code", "customsCode",
              "motCode", "maxRecords")

    def fetch(self, query: dict[str, Any]) -> RawFetch:
        freq = query.get("freqCode", "A")
        params = {k: query[k] for k in self.PARAMS if k in query}
        if self.api_key:
            url, headers, mode = self.DATA_URL.format(freq=freq), {"Ocp-Apim-Subscription-Key": self.api_key}, "live"
        else:
            url, headers, mode = self.PREVIEW_URL.format(freq=freq), {}, "live-preview"
        raw = self._http("GET", url, params=params, headers=headers)
        return RawFetch(raw, {"url": url, "params": params, "authenticated": bool(self.api_key)}, mode)

    @staticmethod
    def _period_end(period: str) -> Optional[datetime]:
        try:
            if len(period) == 4:
                return datetime(int(period), 12, 31, tzinfo=timezone.utc)
            if len(period) == 6:
                y, m = int(period[:4]), int(period[4:])
                nxt = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc)
                return nxt - timedelta(days=1)
        except ValueError:
            return None
        return None

    def normalize(self, obj: Any, retrieved_at: datetime) -> Normalized:
        out = Normalized()
        rows = (obj.get("data") or []) if isinstance(obj, dict) else []
        out.record_count = len(rows)
        for i, r in enumerate(rows):
            period, cmd = str(r.get("period", "")), str(r.get("cmdCode", ""))
            published = self._period_end(period)
            value = _dec(r.get("primaryValue"))
            if published is None or not cmd:
                out.warnings.append(f"row {i}: missing period or cmdCode")
                continue
            rep, par, flow = r.get("reporterCode"), r.get("partnerCode"), r.get("flowCode")
            out.observations.append(MarketObservation(
                evidence_id=f"COMTRADE-{rep}-{par}-{cmd}-{flow}-{period}", observation_type="TRADE_STATISTIC",
                source_connector="UN_COMTRADE", source_record_id=f"{rep}/{par}/{cmd}/{flow}/{period}",
                source_url=(f"https://comtradeplus.un.org/TradeFlow?Frequency={r.get('freqCode', 'A')}&Flows={flow}"
                            f"&CommodityCodes={cmd}&Partners={par}&Reporters={rep}&period={period}"),
                buyer_country=ISO3_TO_ISO2.get(str(r.get("reporterISO", "")).upper()),
                title=f"HS {cmd} {r.get('flowDesc') or flow} — {r.get('reporterDesc') or rep}, {period}",
                classification_codes={"hs": [cmd]}, published_at=published,
                amount={"amount": value, "currency": "USD"} if value is not None else None,
                amount_kind="TRADE_VALUE" if value is not None else None))
        return out


# ----------------------------------------------------------------------------------------------
class SamGovConnector(Connector):
    name, source_connector, key_env = "sam_gov", "SAM_GOV", "SAM_GOV_API_KEY"
    URL = "https://api.sam.gov/opportunities/v2/search"
    PARAMS = ("postedFrom", "postedTo", "limit", "offset", "ptype", "solnum", "noticeid", "title", "state", "zip",
              "ncode", "ccode", "typeOfSetAside", "organizationName", "rdlfrom", "rdlto")

    def fetch(self, query: dict[str, Any]) -> RawFetch:
        if not self.api_key:
            raise ConnectorError(503, "CONNECTOR_NOT_CONFIGURED", "SAM_GOV_API_KEY is not configured")
        params = {k: query[k] for k in self.PARAMS if k in query}
        if "postedFrom" not in params or "postedTo" not in params:
            raise ConnectorError(422, "QUERY_INVALID", "SAM.gov requires postedFrom and postedTo (MM/dd/yyyy)")
        params["limit"] = min(int(params.get("limit", 100)), 1000)
        raw = self._http("GET", self.URL, params={**params, "api_key": self.api_key})
        return RawFetch(raw, {"url": self.URL, "params": params, "authenticated": True}, "live")

    def normalize(self, obj: Any, retrieved_at: datetime) -> Normalized:
        out = Normalized()
        rows = (obj.get("opportunitiesData") or []) if isinstance(obj, dict) else []
        out.record_count = len(rows)
        for i, o in enumerate(rows):
            nid = o.get("noticeId")
            posted = _dt(o.get("postedDate"))
            if not nid or posted is None:
                out.warnings.append(f"row {i}: missing noticeId or postedDate")
                continue
            award = o.get("award") or {}
            kind = f"{o.get('type') or ''} {o.get('baseType') or ''}".lower()
            is_award = "award" in kind
            amount = _dec(award.get("amount")) if is_award else None
            path = (o.get("fullParentPathName") or o.get("department") or "").split(".")
            buyer = path[0].strip() if path and path[0].strip() else None
            codes: dict[str, list[str]] = {}
            if o.get("naicsCode"):
                codes["naics"] = [str(o["naicsCode"])]
            if o.get("classificationCode"):
                codes["psc"] = [str(o["classificationCode"])]
            if o.get("typeOfSetAside"):
                codes["set_aside"] = [str(o["typeOfSetAside"])]
            ids = {"sam_org_code": str(o["fullParentPathCode"]).split(".")[0]} if o.get("fullParentPathCode") else {}
            out.observations.append(MarketObservation(
                evidence_id=f"SAMGOV-{nid}", observation_type="CONTRACT_AWARD" if is_award else "TENDER_NOTICE",
                source_connector="SAM_GOV", source_record_id=o.get("solicitationNumber") or nid,
                source_url=o.get("uiLink") or f"https://sam.gov/opp/{nid}/view", buyer_name=buyer,
                buyer_identifiers=ids, buyer_country="US", buyer_type="PUBLIC", title=o.get("title"),
                classification_codes=codes, published_at=posted, deadline_at=_dt(o.get("responseDeadLine")),
                amount={"amount": amount, "currency": "USD"} if amount is not None else None,
                amount_kind="AWARD_VALUE" if amount is not None else None))
        return out


# ----------------------------------------------------------------------------------------------
class TedConnector(Connector):
    name, source_connector, key_env = "ted_eu", "TED_EU", "TED_API_KEY"
    URL = "https://api.ted.europa.eu/v3/notices/search"
    FIELDS = ["publication-number", "notice-title", "buyer-name", "buyer-country", "notice-type", "publication-date",
              "classification-cpv", "deadline-receipt-tender-date-lot", "total-value", "total-value-cur",
              "estimated-value-glo", "estimated-value-cur-glo"]

    def fetch(self, query: dict[str, Any]) -> RawFetch:
        if not query.get("query"):
            raise ConnectorError(422, "QUERY_INVALID", "TED search needs an expert 'query' string")
        body = {"query": query["query"], "fields": query.get("fields", self.FIELDS),
                "page": int(query.get("page", 1)), "limit": min(int(query.get("limit", 50)), 250),
                "scope": query.get("scope", "ACTIVE")}
        raw = self._http("POST", self.URL, json_body=body)
        return RawFetch(raw, {"url": self.URL, "body": body, "authenticated": False}, "live")

    def normalize(self, obj: Any, retrieved_at: datetime) -> Normalized:
        out = Normalized()
        rows = (obj.get("notices") or []) if isinstance(obj, dict) else []
        out.record_count = len(rows)
        for i, n in enumerate(rows):
            pub = n.get("publication-number")
            published = _dt(_first(n.get("publication-date")))
            if not pub or published is None:
                out.warnings.append(f"row {i}: missing publication-number or publication-date")
                continue
            ntype = str(_first(n.get("notice-type")) or "").lower()
            if ntype.startswith("can"):
                otype, value, ccy, kind = ("CONTRACT_AWARD", n.get("total-value"), n.get("total-value-cur"),
                                           "AWARD_VALUE")
            elif ntype.startswith("pin"):
                otype, value, ccy, kind = ("BUDGET_DOCUMENT", n.get("estimated-value-glo"),
                                           n.get("estimated-value-cur-glo"), "BUDGET")
            else:
                otype, value, ccy, kind = ("TENDER_NOTICE", n.get("estimated-value-glo"),
                                           n.get("estimated-value-cur-glo"), "ESTIMATED_VALUE")
            amount, currency = _dec(_first(value)), _first(ccy)
            country3 = str(_first(n.get("buyer-country")) or "").upper()
            out.observations.append(MarketObservation(
                evidence_id=f"TED-{pub}", observation_type=otype, source_connector="TED_EU", source_record_id=pub,
                source_url=f"https://ted.europa.eu/en/notice/-/detail/{pub}", buyer_name=_ml(n.get("buyer-name")),
                buyer_country=ISO3_TO_ISO2.get(country3, country3[:2] if len(country3) == 2 else None),
                buyer_type="PUBLIC", title=_ml(n.get("notice-title")),
                classification_codes={"cpv": [str(c) for c in (n.get("classification-cpv") or [])]},
                published_at=published, deadline_at=_dt(_first(n.get("deadline-receipt-tender-date-lot"))),
                budget_status="PLANNED" if otype == "BUDGET_DOCUMENT" else None,
                amount={"amount": amount, "currency": currency} if amount is not None and currency else None,
                amount_kind=kind if amount is not None and currency else None))
        return out


# ----------------------------------------------------------------------------------------------
class CpppDropZoneConnector(Connector):
    """India CPPP via an authorized drop zone. Deliberately has no HTTP client: no scraping."""
    name, source_connector, key_env = "india_cppp", "INDIA_CPPP", None

    def __init__(self, dropzone: Optional[str], **kw: Any) -> None:
        super().__init__(**kw)
        self.dropzone = Path(dropzone).resolve() if dropzone else None

    def _inside(self, rel: str) -> Path:
        p = PurePosixPath(rel.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts:
            raise ConnectorError(422, "PATH_TRAVERSAL", f"{rel!r} escapes the drop zone")
        return self.dropzone / Path(*p.parts)

    def fetch(self, query: dict[str, Any]) -> RawFetch:
        if self.dropzone is None or not self.dropzone.is_dir():
            raise ConnectorError(503, "CONNECTOR_NOT_CONFIGURED", "N1_CPPP_DROPZONE is not configured")
        names = query.get("manifests") or sorted(p.name for p in self.dropzone.glob("*.json"))
        manifests = []
        for name in names:
            path = self._inside(name)
            m = json.loads(path.read_text(encoding="utf-8"))
            m["_manifest"] = name
            m["_attachment_check"] = self._check_attachments(m)
            manifests.append(m)
        raw = json.dumps({"manifests": manifests}, sort_keys=True, ensure_ascii=False).encode()
        return RawFetch(raw, {"dropzone": self.dropzone.name, "manifests": names}, "drop_zone")

    def _check_attachments(self, m: dict) -> list[dict]:
        results = []
        tender = str(m.get("tender_id", ""))
        for a in m.get("attachments") or []:
            path = self._inside(f"{tender}/{a.get('name', '')}")
            present, note = path.is_file(), "ok"
            if present and a.get("sha256") and sha256_hex(path.read_bytes()) != a["sha256"]:
                present, note = False, "sha256 mismatch"
            elif not present:
                note = "file missing"
            results.append({"name": a.get("name"), "present": present, "note": note})
        return results

    def normalize(self, obj: Any, retrieved_at: datetime) -> Normalized:
        out = Normalized()
        manifests = (obj.get("manifests") or []) if isinstance(obj, dict) else []
        out.record_count = len(manifests)
        for m in sorted(manifests, key=lambda m: (str(m.get("tender_id")), int(m.get("corrigendum_seq", 0)))):
            tid, published = m.get("tender_id"), _dt(m.get("published_date"))
            if not tid or published is None:
                out.warnings.append(f"{m.get('_manifest')}: missing tender_id or published_date")
                continue
            checks = {c["name"]: c for c in m.get("_attachment_check") or []}
            attachments = [TenderAttachment(name=a["name"], sha256=a.get("sha256"), required=bool(a.get("required")),
                                            present=checks.get(a["name"], {}).get("present", False))
                           for a in m.get("attachments") or []]
            for a in attachments:
                if a.required and not a.present:
                    out.warnings.append(f"{tid}: mandatory attachment {a.name} {checks.get(a.name, {}).get('note')}")
            value = m.get("tender_value") or {}
            amount = _dec(value.get("amount"))
            seq = int(m.get("corrigendum_seq", 0))
            org = str(m.get("organisation") or "").split("|")[0].strip() or None
            portal = m.get("portal_url")
            out.observations.append(MarketObservation(
                evidence_id=f"CPPP-{tid}-C{seq}", observation_type="TENDER_NOTICE", source_connector="INDIA_CPPP",
                source_record_id=tid, source_url=portal if portal and str(portal).startswith("https://") else None,
                buyer_name=org, buyer_country="IN", buyer_type="PUBLIC", title=m.get("title"),
                buyer_identifiers={"cppp_org": org.upper()} if org else {},
                service_category=m.get("service_category"), published_at=published,
                deadline_at=_dt(m.get("bid_submission_end_date")), corrigendum_seq=seq,
                amount={"amount": amount, "currency": value.get("currency", "INR")} if amount is not None else None,
                amount_kind="ESTIMATED_VALUE" if amount is not None else None, quantity=m.get("quantity"),
                requirements=m.get("requirements") or [], compliance_standards=m.get("compliance_standards") or [],
                attachments=attachments))
        return out


# ----------------------------------------------------------------------------------------------
class CrmConnector(Connector):
    """CRM / interview exports → BuyerRecord (submitted to N0 as a BUYER_RECORD artifact)."""
    name, source_connector, key_env = "crm", "CRM", None

    def parse_raw(self, raw: bytes) -> Any:
        text = raw.decode("utf-8-sig")
        if text.lstrip().startswith(("{", "[")):
            return json.loads(text)
        rows = list(csv.DictReader(io.StringIO(text)))
        return {"records": rows}

    def normalize(self, obj: Any, retrieved_at: datetime) -> Normalized:
        out = Normalized()
        rows = obj.get("records") if isinstance(obj, dict) else obj
        rows = rows or []
        out.record_count = len(rows)
        for i, r in enumerate(rows):
            try:
                cats = r.get("service_categories") or []
                if isinstance(cats, str):
                    cats = [c.strip() for c in cats.split(";") if c.strip()]
                budget = r.get("stated_budget")
                if isinstance(budget, str) or budget is None:
                    amt = _dec(r.get("stated_budget_amount") or budget)
                    budget = {"amount": amt, "currency": r.get("stated_budget_currency") or "USD"} if amt else None
                ids = r.get("buyer_identifiers") or {}
                if isinstance(ids, str):
                    ids = dict(kv.split("=", 1) for kv in ids.split(";") if "=" in kv)
                out.buyer_records.append(BuyerRecord(
                    record_id=r["record_id"], record_type=r.get("record_type") or "CRM_ACCOUNT",
                    buyer_name=r["buyer_name"], buyer_identifiers=ids, buyer_country=r.get("buyer_country") or None,
                    buyer_type=r.get("buyer_type") or None, relationship_stage=r.get("relationship_stage") or "NONE",
                    recorded_at=_dt(r.get("recorded_at")) or retrieved_at, service_categories=cats,
                    stated_need=r.get("stated_need") or None, stated_budget=budget,
                    budget_status=r.get("budget_status") or None,
                    do_not_contact=str(r.get("do_not_contact", "")).lower() in ("1", "true", "yes"),
                    source_url=r.get("source_url") or None))
            except (KeyError, ValueError) as exc:
                out.warnings.append(f"record {i}: {type(exc).__name__}: {exc}")
        return out


CONNECTORS: dict[str, type[Connector]] = {"un_comtrade": ComtradeConnector, "sam_gov": SamGovConnector,
                                          "ted_eu": TedConnector, "india_cppp": CpppDropZoneConnector,
                                          "crm": CrmConnector}


def make_connector(name: str, settings, transport: Optional[httpx.BaseTransport] = None,
                   limiter: Optional[RateLimiter] = None) -> Connector:
    cls = CONNECTORS.get(name)
    if cls is None:
        raise ConnectorError(404, "UNKNOWN_CONNECTOR", f"no connector {name!r}; known: {sorted(CONNECTORS)}")
    key_attr = {"un_comtrade": "comtrade_api_key", "sam_gov": "sam_gov_api_key", "ted_eu": "ted_api_key"}.get(name)
    secret = getattr(settings, key_attr, None) if key_attr else None
    kw = dict(api_key=secret.get_secret_value() if secret else None, transport=transport, timeout=settings.http_timeout_s,
              limiter=limiter or RateLimiter(settings.rate_limits.get(name, 1.0)))
    if cls is CpppDropZoneConnector:
        return cls(settings.cppp_dropzone, **kw)
    return cls(**kw)
