"""Append-only, hash-chained audit log (per tenant).

Each event's hash covers the previous event's hash plus the event body, so any
edit or deletion of a stored row is detectable with `verify_chain`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import AuditEvent

GENESIS = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def jsonable(value: Any) -> Any:
    """Round-trip through JSON so Decimals/datetimes become strings before storage."""
    return json.loads(canonical_json(value))


def _event_hash(prev_hash: str, ev: AuditEvent) -> str:
    body = {
        "tenant_id": ev.tenant_id, "seq": ev.seq, "actor": ev.actor, "action": ev.action,
        "entity_type": ev.entity_type, "entity_id": ev.entity_id, "payload": ev.payload,
        "created_at": ev.created_at.isoformat(),
    }
    return hashlib.sha256((prev_hash + canonical_json(body)).encode()).hexdigest()


def record(db: Session, tenant_id: str, actor: str, action: str, entity_type: str,
           entity_id: Any, payload: dict | None = None) -> AuditEvent:
    last = db.scalars(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.seq.desc()).limit(1)
    ).first()
    ev = AuditEvent(
        tenant_id=tenant_id, seq=(last.seq + 1) if last else 1, actor=actor, action=action,
        entity_type=entity_type, entity_id=str(entity_id), payload=jsonable(payload or {}),
        prev_hash=last.hash if last else GENESIS, created_at=utcnow(),
    )
    ev.hash = _event_hash(ev.prev_hash, ev)
    db.add(ev)
    db.flush()
    return ev


def verify_chain(db: Session, tenant_id: str) -> dict:
    events = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.seq)).all()
    prev = GENESIS
    for expected_seq, ev in enumerate(events, start=1):
        if ev.seq != expected_seq or ev.prev_hash != prev or _event_hash(prev, ev) != ev.hash:
            return {"valid": False, "events": len(events), "broken_at_seq": ev.seq}
        prev = ev.hash
    return {"valid": True, "events": len(events), "broken_at_seq": None, "head": prev}
