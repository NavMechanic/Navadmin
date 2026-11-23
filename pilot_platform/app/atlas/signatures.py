"""HMAC-signed vendor callbacks (reference route ING-03 / API A09).

Headers:
    X-Athena-Timestamp: unix seconds
    X-Athena-Nonce:     unique per request (replay protection)
    X-Athena-Signature: sha256=<hex HMAC-SHA256(secret, f"{timestamp}.{nonce}." + raw_body)>
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.security import register_auth_dependency
from ..db import get_db
from ..models import VendorConnector, WebhookNonce

_NONCE = re.compile(r"^[A-Za-z0-9_.-]{8,100}$")


class SignatureError(Exception):
    pass


def sign(secret: str, timestamp: int, nonce: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify(secret: str, headers, body: bytes, tolerance: int, now: float | None = None) -> tuple[int, str]:
    ts, nonce, sig = headers.get("x-athena-timestamp"), headers.get("x-athena-nonce"), headers.get("x-athena-signature")
    if not (ts and nonce and sig):
        raise SignatureError("missing signature headers")
    if not ts.isdigit() or not _NONCE.match(nonce):
        raise SignatureError("malformed timestamp or nonce")
    if abs((now if now is not None else time.time()) - int(ts)) > tolerance:
        raise SignatureError("timestamp outside the allowed window")
    if not hmac.compare_digest(sign(secret, int(ts), nonce, body), sig):
        raise SignatureError("signature mismatch")
    return int(ts), nonce


@dataclass
class VerifiedCallback:
    tenant_id: str
    connector_id: str
    body: bytes
    nonce: str


async def verified_vendor_callback(request: Request, tenant_id: str, connector_id: str,
                                   db: Session = Depends(get_db)) -> VerifiedCallback:
    settings = request.app.state.settings
    body = await request.body()
    if len(body) > settings.vendor_callback_max_bytes:
        raise HTTPException(413, "callback body too large")
    conn = db.scalars(select(VendorConnector).where(VendorConnector.tenant_id == tenant_id,
                                                    VendorConnector.connector_id == connector_id)).first()
    if conn is None or conn.state != "active" or not conn.webhook_secret:
        raise HTTPException(401, "invalid signature")  # do not reveal which connectors exist
    try:
        _, nonce = verify(conn.webhook_secret, request.headers, body, settings.webhook_tolerance_seconds)
    except SignatureError:
        raise HTTPException(401, "invalid signature")
    db.add(WebhookNonce(tenant_id=tenant_id, connector_id=connector_id, nonce=nonce))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "replayed nonce")
    return VerifiedCallback(tenant_id, connector_id, body, nonce)


register_auth_dependency(verified_vendor_callback)
