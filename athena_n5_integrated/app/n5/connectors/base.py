"""Shared plumbing for the N5 supplier connectors.

Licensing / ToS posture (enforced in code, not only documented):

* **Feature flag.** Every live call is refused unless ``N5_ENABLE_LIVE_APIS=true``.
* **On demand, project scoped.** The service only looks up MPNs of an *active* BOM intake of the
  caller's tenant, one MPN per call, at most ``max_lookups_per_line`` per connector per line per TTL
  window. There is no search, crawl or bulk endpoint.
* **TTL cache in memory only.** Responses are cached per tenant + project + connector + key for at
  most the connector's ``max_ttl_s``. Raw payloads are never written to the database or object store.
  Only the fields N5 needs for the project's decision record are kept, with the response's SHA-256.
* **No training, no redistribution.** Derived records carry ``training_allowed: false`` and
  ``redistribution: INTERNAL_PROJECT_ONLY``.
* **Rate limits.** A token bucket per connector (calls per minute) and a daily quota, both refusing
  rather than queueing (``RATE_LIMITED``).
* **Secrets** come from settings (environment / platform secret store) and are redacted from every
  error message.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

import httpx

from ...rules import sha256_hex


class ConnectorError(Exception):
    def __init__(self, code: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class Policy:
    name: str
    terms: str
    max_ttl_s: int
    persist_raw: bool = False
    training_allowed: bool = False


@dataclass
class LookupResult:
    connector: str
    key: str
    offers: list[dict[str, Any]]
    http_status: Optional[int]
    response_sha256: Optional[str]
    retrieved_at: datetime
    cache_hit: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class RateLimiter:
    """Non-blocking token bucket (``per_minute``) plus a rolling 24 h quota. ``acquire`` raises
    ``RATE_LIMITED`` instead of sleeping inside a request."""

    def __init__(self, per_minute: int, per_day: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate, self.capacity, self.per_day, self.clock = per_minute / 60.0, float(max(per_minute, 1)), per_day, clock
        self.tokens, self.updated = self.capacity, clock()
        self.day: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            self.day = [t for t in self.day if now - t < 86_400]
            if len(self.day) >= self.per_day:
                raise ConnectorError("RATE_LIMITED", f"daily quota of {self.per_day} calls reached", 429)
            if self.tokens < 1:
                raise ConnectorError("RATE_LIMITED", "per-minute rate limit reached; retry later", 429)
            self.tokens -= 1
            self.day.append(now)


class TTLCache:
    """In-memory only. Keys are (tenant, project, connector, lookup key)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_entries: int = 5000) -> None:
        self.clock, self.max_entries = clock, max_entries
        self._d: dict[tuple, tuple[float, LookupResult]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple) -> Optional[LookupResult]:
        with self._lock:
            hit = self._d.get(key)
            if hit is None or hit[0] <= self.clock():
                self._d.pop(key, None)
                return None
            return hit[1]

    def put(self, key: tuple, value: LookupResult, ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        with self._lock:
            if len(self._d) >= self.max_entries:
                now = self.clock()
                self._d = {k: v for k, v in self._d.items() if v[0] > now}
                if len(self._d) >= self.max_entries:
                    self._d.pop(next(iter(self._d)))
            self._d[key] = (self.clock() + ttl_s, value)

    def __len__(self) -> int:
        return len(self._d)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def dec(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def money(text: Any) -> Optional[Decimal]:
    """Parse a distributor price such as ``$0.10``, ``₹6.50``, ``1.234,56 €`` or ``0,10`` exactly."""
    if isinstance(text, (int, Decimal)):
        return Decimal(text)
    s = "".join(ch for ch in str(text or "") if ch.isdigit() or ch in ".,")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.rsplit(",", 1)[1]) != 3 else s.replace(",", "")
    return dec(s)


def loads(raw: bytes) -> Any:
    """JSON with exact decimals (never binary floats)."""
    return json.loads(raw, parse_float=Decimal)


class HttpConnector:
    policy: Policy
    name = "BASE"

    def __init__(self, settings, transport: Optional[httpx.BaseTransport] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.settings, self.transport, self.clock = settings, transport, clock

    def secrets(self) -> list[str]:  # pragma: no cover - overridden
        return []

    def configured(self) -> bool:  # pragma: no cover - overridden
        return False

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=self.settings.http_timeout_s,
                            headers={"User-Agent": "athena-n5/1.0 (procurement decision support)"})

    def request(self, method: str, url: str, **kw: Any) -> tuple[int, bytes]:
        try:
            with self._client() as c:
                r = c.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise ConnectorError("UPSTREAM_ERROR", redact(f"{self.name}: {type(exc).__name__}: {exc}",
                                                          self.secrets())) from None
        if r.status_code == 404:
            return 404, r.content
        if r.status_code == 429:
            raise ConnectorError("RATE_LIMITED", f"{self.name} answered 429 Too Many Requests", 429)
        if r.status_code >= 400:
            raise ConnectorError("UPSTREAM_ERROR", redact(f"{self.name} answered HTTP {r.status_code}: "
                                                          f"{r.text[:300]}", self.secrets()), r.status_code)
        return r.status_code, r.content

    def result(self, key: str, offers: list[dict], status: int, raw: bytes, **meta: Any) -> LookupResult:
        return LookupResult(connector=self.name, key=key, offers=offers, http_status=status,
                            response_sha256=sha256_hex(raw), retrieved_at=self.clock(), meta=meta)
