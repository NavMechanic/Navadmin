"""N8 Lifecycle — build/lot/test/warranty telemetry (gate R: "No synthetic data presented as actual").

* Every event must declare `is_synthetic` explicitly; there is no default.
* Replayed events are generated with is_synthetic=True and source="synthetic_replay".
* Summaries report actual metrics from real events only; synthetic counts appear
  separately and only when asked for.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.errors import ValidationFailed
from ..core.security import Principal
from ..db import utcnow
from ..models import TelemetryEvent

EVENT_TYPES = ("build", "lot", "test", "warranty")
REPLAY_SOURCE = "synthetic_replay"


def record_events(db: Session, principal: Principal, events: list[dict], source: str = "api") -> list[TelemetryEvent]:
    if source == REPLAY_SOURCE:
        raise ValidationFailed(f"source {REPLAY_SOURCE!r} is reserved for the replay generator")
    rows = []
    for i, e in enumerate(events):
        if e.get("event_type") not in EVENT_TYPES:
            raise ValidationFailed(f"events[{i}].event_type must be one of {EVENT_TYPES}")
        if not isinstance(e.get("is_synthetic"), bool):
            raise ValidationFailed(f"events[{i}].is_synthetic must be declared true or false")
        if not e.get("serial"):
            raise ValidationFailed(f"events[{i}].serial is required")
        try:
            occurred = datetime.fromisoformat(e["occurred_at"]).replace(tzinfo=None) if e.get("occurred_at") else utcnow()
        except ValueError:
            raise ValidationFailed(f"events[{i}].occurred_at must be ISO-8601")
        rows.append(TelemetryEvent(tenant_id=principal.tenant_id, event_type=e["event_type"], serial=e["serial"],
                                   lot=e.get("lot", ""), payload=e.get("payload", {}), is_synthetic=e["is_synthetic"],
                                   source=source, occurred_at=occurred))
    db.add_all(rows)
    db.flush()
    audit.record(db, principal.tenant_id, principal.username, "telemetry.recorded", "telemetry", len(rows),
                 {"synthetic": sum(r.is_synthetic for r in rows), "actual": sum(not r.is_synthetic for r in rows)})
    return rows


def replay_synthetic(db: Session, principal: Principal, units: int, seed: int = 42) -> int:
    if not 1 <= units <= 10_000:
        raise ValidationFailed("units must be 1-10000")
    rng = random.Random(seed)
    start = utcnow() - timedelta(days=90)
    rows = []
    for n in range(units):
        serial, lot = f"SYN-{seed}-{n:05d}", f"SYN-LOT-{n // 100:03d}"
        t = start + timedelta(hours=rng.randint(0, 24 * 30))
        rows.append(("build", serial, lot, {"line": "replay"}, t))
        passed = rng.random() > 0.03
        rows.append(("test", serial, lot, {"result": "pass" if passed else "fail"}, t + timedelta(hours=2)))
        if passed and rng.random() < 0.01:
            rows.append(("warranty", serial, lot, {"claim": "field return"}, t + timedelta(days=rng.randint(10, 60))))
    db.add_all(TelemetryEvent(tenant_id=principal.tenant_id, event_type=et, serial=s, lot=lot, payload=p,
                              is_synthetic=True, source=REPLAY_SOURCE, occurred_at=t) for et, s, lot, p, t in rows)
    db.flush()
    audit.record(db, principal.tenant_id, principal.username, "telemetry.replayed", "telemetry", len(rows), {"seed": seed})
    return len(rows)


def _counts(db: Session, tenant_id: str, synthetic: bool) -> dict:
    rows = db.execute(select(TelemetryEvent.event_type, func.count()).where(
        TelemetryEvent.tenant_id == tenant_id, TelemetryEvent.is_synthetic.is_(synthetic))
        .group_by(TelemetryEvent.event_type)).all()
    return {et: 0 for et in EVENT_TYPES} | dict(rows)


def _first_pass_yield(db: Session, tenant_id: str, synthetic: bool) -> float | None:
    tests = db.scalars(select(TelemetryEvent).where(TelemetryEvent.tenant_id == tenant_id,
                                                    TelemetryEvent.event_type == "test",
                                                    TelemetryEvent.is_synthetic.is_(synthetic))).all()
    if not tests:
        return None
    return round(sum(1 for t in tests if t.payload.get("result") == "pass") / len(tests), 4)


def summary(db: Session, tenant_id: str, include_synthetic: bool = False) -> dict:
    out = {"actual": {"counts": _counts(db, tenant_id, False), "first_pass_yield": _first_pass_yield(db, tenant_id, False)},
           "data_basis": "actual events only"}
    if include_synthetic:
        out["synthetic"] = {"counts": _counts(db, tenant_id, True), "first_pass_yield": _first_pass_yield(db, tenant_id, True),
                            "label": "SYNTHETIC REPLAY — not measured data"}
        out["data_basis"] = "actual and synthetic shown separately; never combined"
    return out


def mislabelled_replay_count(db: Session, tenant_id: str) -> int:
    return db.scalar(select(func.count()).select_from(TelemetryEvent).where(
        TelemetryEvent.tenant_id == tenant_id, TelemetryEvent.source == REPLAY_SOURCE,
        TelemetryEvent.is_synthetic.is_(False))) or 0
