"""CAD/EDA vault webhook ingestion.

The vault pushes a revision event; this turns it into a ``PartRevision`` and an
``Artifact`` that the gate rules can read. Three properties matter more than the
mapping itself:

* **Signature verification.** The payload decides whether a gate passes, so an
  unsigned webhook is an unauthenticated write to the gate criteria. HMAC-SHA256
  over the raw body, compared in constant time, with a timestamp window to stop
  replay.
* **Idempotency.** Vaults retry. Ingesting the same ``event_id`` twice must not
  create a second revision or a duplicate artifact.
* **Change detection.** A revision arriving against a part that is already inside
  a released structure is an engineering change, not a quiet update. The
  ingester says so and can raise the ECN automatically.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..enums import (ArtifactStatus, ArtifactType, ChangeType, ECNCategory,
                     LifecycleState)
from ..models import Artifact, AuditEvent, Part, PartRevision, utcnow
from ..schemas import VaultRevisionPayload
from . import bom as bom_svc
from . import ecn as ecn_svc

log = logging.getLogger(__name__)


class WebhookError(Exception):
    """Raised for signature, replay or mapping failures."""


def verify_signature(raw_body: bytes, signature: str | None,
                     timestamp: str | None) -> None:
    """Verify an HMAC-SHA256 webhook signature.

    Accepts ``sha256=<hex>`` or a bare hex digest. Raises ``WebhookError`` with a
    reason the integrator can act on.
    """
    s = get_settings()
    if not signature:
        raise WebhookError(
            "Missing X-Athena-Signature. This endpoint writes the evidence gate "
            "decisions are made from; unsigned payloads are refused."
        )
    if timestamp:
        try:
            sent = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WebhookError(f"X-Athena-Timestamp is not RFC 3339: {timestamp!r}") from exc
        skew = abs((datetime.now(timezone.utc) - sent).total_seconds())
        if skew > s.webhook_max_skew_seconds:
            raise WebhookError(
                f"Timestamp is {skew:.0f}s from now, outside the "
                f"{s.webhook_max_skew_seconds}s window — refusing as a possible replay."
            )
    provided = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
    expected = hmac.new(s.cad_vault_webhook_secret.encode(), raw_body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided.lower(), expected):
        raise WebhookError("Signature does not match the configured vault secret.")


def sign(raw_body: bytes) -> str:
    """Produce a signature for a payload — used by the demo and the tests."""
    s = get_settings()
    return "sha256=" + hmac.new(s.cad_vault_webhook_secret.encode(), raw_body,
                                hashlib.sha256).hexdigest()


async def ingest_revision(session: AsyncSession, payload: VaultRevisionPayload, *,
                          actor: str = "cad-vault",
                          auto_ecn: bool = True,
                          request_id: str | None = None) -> dict:
    """Ingest one revision event."""
    # ---- idempotency: has this event already been recorded? -------------
    prior = (await session.execute(
        select(AuditEvent).where(AuditEvent.action == "vault.ingest",
                                 AuditEvent.object_ref == payload.event_id)
    )).scalar_one_or_none()
    if prior is not None:
        return {"accepted": True, "duplicate": True,
                "part_number": payload.part_number, "revision": payload.revision,
                "revision_id": prior.detail.get("revision_id"),
                "artifact_id": prior.detail.get("artifact_id"),
                "created_revision": False, "created_artifact": False,
                "triggered_ecn": prior.detail.get("triggered_ecn"),
                "message": f"Event {payload.event_id} was already ingested; no change made."}

    part = (await session.execute(
        select(Part).where(Part.part_number == payload.part_number))).scalar_one_or_none()
    if part is None:
        raise WebhookError(
            f"Unknown part_number {payload.part_number!r}. The vault is authoritative for "
            "geometry, not for the part master — create the part first so it carries a "
            "supply mode, supplier and lifecycle risk."
        )

    # ---- revision ------------------------------------------------------
    revision = (await session.execute(
        select(PartRevision).where(PartRevision.part_id == part.id,
                                   PartRevision.rev == payload.revision)
    )).scalar_one_or_none()
    created_revision = False
    if revision is None:
        current = (await session.execute(
            select(PartRevision).where(PartRevision.part_id == part.id)
            .order_by(PartRevision.id.desc()).limit(1))).scalar_one_or_none()
        revision = PartRevision(
            part_id=part.id, rev=payload.revision,
            lifecycle_state=LifecycleState.WIP,
            change_summary=payload.change_summary,
            supersedes_id=current.id if current else None,
            source_system=payload.attributes.get("source_system", "cad-vault"),
            source_ref=payload.uri or payload.file_name,
        )
        session.add(revision)
        await session.flush()
        created_revision = True

    if payload.event_type == "revision.released":
        revision.lifecycle_state = LifecycleState.RELEASED
        revision.released_at = payload.occurred_at
        revision.released_by = payload.author or actor
        # The revision it supersedes is no longer current.
        if revision.supersedes_id:
            old = await session.get(PartRevision, revision.supersedes_id)
            if old is not None and old.lifecycle_state == LifecycleState.RELEASED:
                old.lifecycle_state = LifecycleState.SUPERSEDED

    # ---- artifact -------------------------------------------------------
    artifact = (await session.execute(
        select(Artifact).where(Artifact.name == payload.file_name,
                               Artifact.revision == payload.revision)
    )).scalar_one_or_none()
    created_artifact = False
    skus = await bom_svc.affected_skus(session, part.part_number)
    sku_for_artifact = (part.part_number if part.is_sellable
                        else (skus[0] if skus else None))
    if artifact is None:
        artifact = Artifact(
            name=payload.file_name, artifact_type=payload.artifact_type.value,
            revision=payload.revision, part_id=part.id, sku=sku_for_artifact,
            uri=payload.uri, checksum_sha256=payload.checksum_sha256,
            file_format=payload.file_format, source_system="cad-vault",
            ingested_at=utcnow(), is_parametric=payload.is_parametric,
            attributes=payload.attributes,
            status=(ArtifactStatus.RELEASED if payload.event_type == "revision.released"
                    else ArtifactStatus.DRAFT),
        )
        if payload.event_type == "revision.released":
            artifact.released_at = payload.occurred_at
        session.add(artifact)
        await session.flush()
        created_artifact = True
    else:
        if artifact.checksum_sha256 and artifact.checksum_sha256 != payload.checksum_sha256:
            # Same name and revision, different content. Silently overwriting
            # here would break the audit trail for a released document.
            raise WebhookError(
                f"{payload.file_name} rev {payload.revision} already exists with a "
                f"different checksum ({artifact.checksum_sha256[:12]}... vs "
                f"{payload.checksum_sha256[:12]}...). Publish a new revision rather than "
                "replacing a released file in place."
            )
        if payload.event_type == "revision.released":
            artifact.status = ArtifactStatus.RELEASED
            artifact.released_at = payload.occurred_at

    # ---- is this a change to something already released? ----------------
    triggered_ecn: str | None = None
    released_elsewhere = (await session.execute(
        select(PartRevision).where(PartRevision.part_id == part.id,
                                   PartRevision.lifecycle_state.in_(
                                       [LifecycleState.RELEASED, LifecycleState.SUPERSEDED]),
                                   PartRevision.id != revision.id))).scalars().first()
    if auto_ecn and created_revision and released_elsewhere is not None and skus:
        change = await ecn_svc.create_ecn(
            session,
            title=f"Vault revision {payload.part_number} rev {payload.revision}",
            reason=(f"The CAD/EDA vault published rev {payload.revision} of "
                    f"{payload.part_number} ({payload.file_name}). A previously released "
                    "revision exists, so this is an engineering change and its downstream "
                    "effects must be assessed before the new revision is consumed."),
            category=(ECNCategory.SAFETY if part.safety_critical
                      else ECNCategory.DOCUMENTATION),
            raised_by=actor,
            affected=[{"part_number": part.part_number,
                       "change_type": ChangeType.SPEC_CHANGE,
                       "to_revision": payload.revision,
                       "detail": payload.change_summary}],
            primary_sku=skus[0] if skus else None,
            analyse=True, request_id=request_id,
        )
        triggered_ecn = change.ecn_number

    session.add(AuditEvent(
        actor=actor, action="vault.ingest", object_type="VAULT_EVENT",
        object_ref=payload.event_id, request_id=request_id,
        detail={"part_number": part.part_number, "revision": payload.revision,
                "revision_id": revision.id, "artifact_id": artifact.id,
                "artifact_type": payload.artifact_type.value,
                "event_type": payload.event_type, "triggered_ecn": triggered_ecn},
    ))
    await session.commit()

    msg = (f"{'Released' if payload.event_type == 'revision.released' else 'Recorded'} "
           f"{payload.part_number} rev {payload.revision} "
           f"({payload.artifact_type.value}, {payload.file_format}).")
    if triggered_ecn:
        msg += f" Raised {triggered_ecn} — a released revision already existed."
    return {"accepted": True, "duplicate": False, "part_number": part.part_number,
            "revision": payload.revision, "revision_id": revision.id,
            "artifact_id": artifact.id, "created_revision": created_revision,
            "created_artifact": created_artifact, "triggered_ecn": triggered_ecn,
            "message": msg}
