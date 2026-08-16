"""Compliance Vault client — BIS CRS, WPC ETA and UN 38.3 status.

With ``ATHENA_COMPLIANCE_VAULT_BASE_URL`` set, this calls the real vault. With
it unset, it replays the certificate state already held in the database and
marks the result ``replay`` so nobody mistakes an unverified record for a
verified one.

The distinction the gate rules depend on: a certificate is not a boolean. It has
a status *and* an expiry, and one that lapses mid-ramp is a production stop with
a lead time attached. ``certificate_min_validity_days`` is what turns that into
an enforceable rule.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..enums import CertificateStatus, CertificationScheme
from ..models import AuditEvent, Certificate, utcnow

log = logging.getLogger(__name__)


class ComplianceError(RuntimeError):
    """Upstream vault failure. Surfaced as 502 — it is not our bug."""


async def _fetch_live(sku: str) -> list[dict[str, Any]]:
    s = get_settings()
    url = f"{s.compliance_vault_base_url.rstrip('/')}/v1/certificates"
    headers = {"accept": "application/json"}
    if s.compliance_vault_api_key:
        headers["authorization"] = f"Bearer {s.compliance_vault_api_key}"
    try:
        async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
            resp = await client.get(url, params={"sku": sku}, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as exc:
        raise ComplianceError(
            f"Compliance Vault returned {exc.response.status_code} for {sku}."
        ) from exc
    except httpx.HTTPError as exc:
        raise ComplianceError(f"Compliance Vault unreachable: {exc}") from exc

    rows = body.get("certificates", body) if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise ComplianceError(
            "Compliance Vault returned an unexpected shape; expected a list of "
            "certificates or {'certificates': [...]}."
        )
    return rows


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        return None


async def refresh(session: AsyncSession, sku: str, *, actor: str | None = None,
                  request_id: str | None = None) -> dict[str, Any]:
    """Re-verify every certificate for a SKU and write back status and expiry."""
    s = get_settings()
    held = (await session.execute(
        select(Certificate).where(Certificate.sku == sku))).scalars().all()
    if not held:
        return {"sku": sku, "mode": "replay" if s.compliance_offline else "live",
                "checked": 0, "updated": 0, "certificates": [],
                "warnings": [f"No certificate records exist for {sku}. Nothing to verify — "
                             "register the expected schemes first, or the compliance gate "
                             "rules have nothing to read."]}

    warnings: list[str] = []
    updated = 0
    now = utcnow()

    if s.compliance_offline:
        for c in held:
            c.last_verified_at = now
            c.verification_source = "replay"
            # Expiry is real time even in replay: a record that has aged past its
            # expiry must not keep reporting VALID just because nobody called out.
            if c.expires_on and c.expires_on < date.today() and c.status == CertificateStatus.VALID:
                c.status = CertificateStatus.EXPIRED
                updated += 1
                warnings.append(
                    f"{c.scheme} for {sku} expired on {c.expires_on.isoformat()} and has "
                    "been marked EXPIRED locally."
                )
        warnings.append(
            "No Compliance Vault configured — statuses are replayed from the local "
            "record and have not been verified against the issuing authority. Set "
            "ATHENA_COMPLIANCE_VAULT_BASE_URL for live verification."
        )
    else:
        remote = {(r.get("scheme"), r.get("certificate_number")): r
                  for r in await _fetch_live(sku)}
        by_scheme = {r.get("scheme"): r for r in remote.values()}
        for c in held:
            r = remote.get((c.scheme, c.certificate_number)) or by_scheme.get(c.scheme)
            if r is None:
                warnings.append(
                    f"{c.scheme} for {sku} is held locally but the vault returned no "
                    "matching record. Treat the local status as unverified."
                )
                c.last_verified_at = now
                c.verification_source = "live-missing"
                continue
            new_status = str(r.get("status", c.status)).upper()
            if new_status not in set(CertificateStatus):
                warnings.append(f"Vault returned unknown status {new_status!r} for "
                                f"{c.scheme}; local status left unchanged.")
                new_status = c.status
            if new_status != c.status:
                updated += 1
            c.status = new_status
            c.certificate_number = r.get("certificate_number") or c.certificate_number
            c.issued_on = _parse_date(r.get("issued_on")) or c.issued_on
            c.expires_on = _parse_date(r.get("expires_on")) or c.expires_on
            c.lab = r.get("lab") or c.lab
            c.vault_ref = r.get("id") or c.vault_ref
            c.last_verified_at = now
            c.verification_source = "live"

    # Forward-looking check the gate rules turn into a hard stop later.
    horizon = date.today() + timedelta(days=s.certificate_min_validity_days)
    for c in held:
        if c.status == CertificateStatus.VALID and c.expires_on and c.expires_on < horizon:
            warnings.append(
                f"{c.scheme} for {sku} expires {c.expires_on.isoformat()}, inside the "
                f"{s.certificate_min_validity_days}-day validity window. Renewal lead "
                "time is measured in weeks — start it now, not at the gate."
            )

    session.add(AuditEvent(actor=actor, action="compliance.refresh", object_type="SKU",
                           object_ref=sku, request_id=request_id,
                           detail={"mode": "replay" if s.compliance_offline else "live",
                                   "checked": len(held), "updated": updated}))
    await session.commit()

    return {"sku": sku, "mode": "replay" if s.compliance_offline else "live",
            "checked": len(held), "updated": updated,
            "certificates": held, "warnings": warnings}
