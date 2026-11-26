"""ECN / ECO workflow and automatic impact analysis.

The valuable part is not the ECN header — it is answering "what else does this
break" without a human remembering. When a battery cell is substituted, the
records that must be re-reviewed are spread across the DFMEA, the PFMEA, the
control plan, the certificates and the released artifacts of every SKU that
contains the cell, including the bundled kit two levels up.

Impact selection is deliberately explicit about *why* each record was pulled in,
because an impact list nobody trusts gets closed without being read:

* **Structural** — the record analyses the changed part, or an assembly that
  contains it. Found through the BOM where-used graph.
* **Safety-adjacent** — a high-severity record on an affected SKU. Conservative
  by design: a cell change alters the assumptions behind the BMS over-charge
  analysis even though the two parts are structural siblings.
* **Derived documents** — released BOM exports and schematics for affected SKUs,
  which are now stale by construction.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..enums import (ArtifactStatus, ArtifactType, ChangeType, ECNCategory, ECNState,
                     EffectivityType, ImpactObjectType, ImpactState, ReviewState)
from ..models import (Artifact, AuditEvent, Certificate, ControlPlanItem,
                      DesignParameter, ECN, ECNAffectedItem, ECNImpact, FmeaRow, Part,
                      PartRevision, utcnow)
from . import bom as bom_svc

log = logging.getLogger(__name__)

# A record at or above this severity on an affected SKU is flagged even without
# a structural link. Chosen to match the AIAG-VDA severity band where a failure
# affects safe operation or regulatory compliance.
SAFETY_SEVERITY_THRESHOLD = 8


class ECNError(Exception):
    """Raised for change-workflow violations."""


async def next_ecn_number(session: AsyncSession, year: int | None = None) -> str:
    year = year or datetime.now(timezone.utc).year
    prefix = f"ECN-{year}-"
    count = (await session.execute(
        select(func.count()).select_from(ECN).where(ECN.ecn_number.like(f"{prefix}%"))
    )).scalar_one()
    return f"{prefix}{count + 1:04d}"


async def create_ecn(session: AsyncSession, *, title: str, reason: str,
                     category: ECNCategory, raised_by: str,
                     affected: list[dict[str, Any]],
                     primary_sku: str | None = None,
                     effectivity_type: EffectivityType = EffectivityType.IMMEDIATE,
                     effectivity_date=None, effectivity_serial: str | None = None,
                     stock_disposition: str | None = None,
                     target_gate: str | None = None,
                     analyse: bool = True,
                     request_id: str | None = None) -> ECN:
    """Raise an ECN against one or more parts."""
    ecn = ECN(
        ecn_number=await next_ecn_number(session), title=title, reason=reason,
        category=category, state=ECNState.DRAFT, primary_sku=primary_sku,
        raised_by=raised_by, raised_at=utcnow(), effectivity_type=effectivity_type,
        effectivity_date=effectivity_date, effectivity_serial=effectivity_serial,
        stock_disposition=stock_disposition, target_gate=target_gate,
    )
    session.add(ecn)
    await session.flush()

    for item in affected:
        part = (await session.execute(
            select(Part).where(Part.part_number == item["part_number"]))
        ).scalar_one_or_none()
        if part is None:
            raise ECNError(f"Unknown part_number {item['part_number']!r}.")
        replacement = None
        if item.get("replacement_part_number"):
            replacement = (await session.execute(
                select(Part).where(Part.part_number == item["replacement_part_number"]))
            ).scalar_one_or_none()
            if replacement is None:
                raise ECNError(
                    f"Replacement part {item['replacement_part_number']!r} does not exist. "
                    "Create it first — an ECN that points at a part number nobody has "
                    "costed or qualified is not actionable."
                )
        to_rev = None
        if item.get("to_revision"):
            to_rev = (await session.execute(
                select(PartRevision).where(PartRevision.part_id == part.id,
                                           PartRevision.rev == item["to_revision"]))
            ).scalar_one_or_none()

        current = (await session.execute(
            select(PartRevision).where(PartRevision.part_id == part.id)
            .order_by(PartRevision.id.desc()).limit(1))).scalar_one_or_none()

        session.add(ECNAffectedItem(
            ecn_id=ecn.id, part_id=part.id,
            change_type=item.get("change_type", ChangeType.SPEC_CHANGE),
            from_revision_id=current.id if current else None,
            to_revision_id=to_rev.id if to_rev else None,
            replacement_part_id=replacement.id if replacement else None,
            detail=item.get("detail"),
        ))

    session.add(AuditEvent(actor=raised_by, action="ecn.create", object_type="ECN",
                           object_ref=ecn.ecn_number, request_id=request_id,
                           detail={"title": title, "category": category}))
    await session.flush()

    if analyse:
        await analyse_impact(session, ecn, actor=raised_by, request_id=request_id)

    await session.commit()
    await session.refresh(ecn)
    return ecn


async def analyse_impact(session: AsyncSession, ecn: ECN, *, actor: str | None = None,
                         request_id: str | None = None) -> list[ECNImpact]:
    """Find every record the change invalidates and flag it Pending Re-Review.

    Idempotent: re-running replaces the impact set but preserves records an
    engineer has already reviewed, so a re-analysis after adding a fourth
    affected part does not silently discard three completed reviews.
    """
    existing = {(i.object_type, i.object_ref): i for i in
                (await session.execute(
                    select(ECNImpact).where(ECNImpact.ecn_id == ecn.id))).scalars().all()}

    items = (await session.execute(
        select(ECNAffectedItem).where(ECNAffectedItem.ecn_id == ecn.id))).scalars().all()
    if not items:
        raise ECNError(f"{ecn.ecn_number} has no affected items to analyse.")

    found: dict[tuple[str, str], dict[str, Any]] = {}

    for item in items:
        part = (await session.execute(
            select(Part).where(Part.id == item.part_id))).scalar_one()

        # ---- structural reach ------------------------------------------
        try:
            used_in = await bom_svc.where_used(session, part.part_number)
        except bom_svc.BomError:
            used_in = []
        ancestor_ids = {part.id}
        paths: dict[str, str] = {part.part_number: part.part_number}
        for entry in used_in:
            anc = (await session.execute(
                select(Part).where(Part.part_number == entry.part_number))).scalar_one()
            ancestor_ids.add(anc.id)
            paths.setdefault(anc.part_number, entry.path)
        skus = await bom_svc.affected_skus(session, part.part_number)
        base_path = paths.get(part.part_number, part.part_number)

        def _add(obj_type: ImpactObjectType, ref: str, title: str, sku: str | None,
                 reason: str, path: str | None = None) -> None:
            found[(obj_type.value, ref)] = {
                "object_type": obj_type.value, "object_ref": ref,
                "object_title": title[:300], "sku": sku, "reason": reason,
                "where_used_path": (path or base_path)[:400],
            }

        # ---- FMEA rows --------------------------------------------------
        fmea_rows = (await session.execute(
            select(FmeaRow).where(
                (FmeaRow.linked_part_id.in_(ancestor_ids)) | (FmeaRow.sku.in_(skus or ["_"])))
        )).scalars().all()
        for row in fmea_rows:
            obj = (ImpactObjectType.DFMEA_ROW if row.fmea_type == "DFMEA"
                   else ImpactObjectType.PFMEA_ROW)
            if row.linked_part_id in ancestor_ids:
                reason = (f"Analyses {part.part_number} or an assembly containing it; "
                          f"the change alters the item this row assesses.")
            elif row.severity >= SAFETY_SEVERITY_THRESHOLD:
                reason = (f"Severity {row.severity} record on {row.sku}, which contains "
                          f"{part.part_number}. Flagged conservatively: a change to a "
                          "constituent part can invalidate a high-severity analysis even "
                          "without a direct structural link.")
            else:
                continue
            _add(obj, row.row_ref, f"{row.item_function} — {row.failure_mode}",
                 row.sku, reason, paths.get(row.sku))

        # ---- control plan ------------------------------------------------
        flagged_fmea = {ref for (t, ref) in found
                        if t in (ImpactObjectType.DFMEA_ROW, ImpactObjectType.PFMEA_ROW)}
        controls = (await session.execute(
            select(ControlPlanItem).where(
                (ControlPlanItem.linked_part_id.in_(ancestor_ids))
                | (ControlPlanItem.linked_fmea_ref.in_(flagged_fmea or ["_"])))
        )).scalars().all()
        for c in controls:
            reason = ("Controls the changed part directly."
                      if c.linked_part_id in ancestor_ids
                      else f"Derived from PFMEA row {c.linked_fmea_ref}, which this "
                           "change has flagged for re-review.")
            _add(ImpactObjectType.CONTROL_PLAN_ITEM, c.control_ref,
                 f"{c.station or ''} {c.characteristic}".strip(), c.sku, reason,
                 paths.get(c.sku))

        # ---- design parameters ------------------------------------------
        params = (await session.execute(
            select(DesignParameter).where(DesignParameter.part_id.in_(ancestor_ids))
        )).scalars().all()
        for p in params:
            _add(ImpactObjectType.DESIGN_PARAMETER, p.code, p.name, p.sku,
                 "Measured on an item this change modifies; the value must be re-verified.",
                 paths.get(p.sku))

        # ---- certificates -------------------------------------------------
        # Only for safety-critical parts. A label artwork change does not
        # invalidate a UN 38.3 summary, and flagging it every time is how impact
        # lists become noise nobody reads.
        if part.safety_critical and skus:
            certs = (await session.execute(
                select(Certificate).where(Certificate.sku.in_(skus)))).scalars().all()
            for c in certs:
                _add(ImpactObjectType.CERTIFICATE, f"{c.scheme}:{c.sku}",
                     f"{c.scheme} {c.certificate_number or '(unissued)'}", c.sku,
                     f"{part.part_number} is safety-critical and is within the certified "
                     f"construction. {c.scheme} is granted against a specific build; a "
                     "substitution normally requires re-testing or a variant filing.",
                     paths.get(c.sku))

        # ---- derived documents ---------------------------------------------
        stale_types = [ArtifactType.BOM_EXPORT]
        if item.change_type in (ChangeType.SUBSTITUTE, ChangeType.ADD, ChangeType.REMOVE):
            stale_types.append(ArtifactType.SCHEMATIC)
        arts = (await session.execute(
            select(Artifact).where(Artifact.sku.in_(skus or ["_"]),
                                   Artifact.artifact_type.in_([t.value for t in stale_types]),
                                   Artifact.status == ArtifactStatus.RELEASED)
        )).scalars().all()
        for a in arts:
            _add(ImpactObjectType.ARTIFACT, f"{a.name}@{a.revision}", a.name, a.sku,
                 "Released document derived from the structure this change modifies; "
                 "it is stale until re-exported.", paths.get(a.sku or ""))

    # ---- persist, preserving completed reviews --------------------------
    for key, data in found.items():
        prior = existing.pop(key, None)
        if prior is not None:
            prior.reason = data["reason"]
            prior.where_used_path = data["where_used_path"]
            continue
        session.add(ECNImpact(ecn_id=ecn.id, state=ImpactState.PENDING_RE_REVIEW, **data))
    # anything previously flagged but no longer in scope
    for stale in existing.values():
        if stale.state == ImpactState.PENDING_RE_REVIEW:
            await session.delete(stale)

    await session.flush()
    await _apply_review_states(session, ecn)

    ecn.impact_analysed_at = utcnow()
    session.add(AuditEvent(
        actor=actor, action="ecn.impact_analysed", object_type="ECN",
        object_ref=ecn.ecn_number, request_id=request_id,
        detail={"impacts": len(found)},
    ))
    await session.flush()

    return list((await session.execute(
        select(ECNImpact).where(ECNImpact.ecn_id == ecn.id)
        .order_by(ECNImpact.object_type, ECNImpact.object_ref))).scalars().all())


async def _apply_review_states(session: AsyncSession, ecn: ECN) -> None:
    """Set Pending Re-Review on the records themselves.

    The impact row is the ECN's view; the ``review_state`` column on the record
    is what the gate engine reads. Both have to move or the gate will not see it.
    """
    impacts = (await session.execute(
        select(ECNImpact).where(ECNImpact.ecn_id == ecn.id,
                                ECNImpact.state == ImpactState.PENDING_RE_REVIEW)
    )).scalars().all()

    fmea_refs = [i.object_ref for i in impacts
                 if i.object_type in (ImpactObjectType.DFMEA_ROW, ImpactObjectType.PFMEA_ROW)]
    control_refs = [i.object_ref for i in impacts
                    if i.object_type == ImpactObjectType.CONTROL_PLAN_ITEM]
    param_refs = [i.object_ref for i in impacts
                  if i.object_type == ImpactObjectType.DESIGN_PARAMETER]

    for row in (await session.execute(
            select(FmeaRow).where(FmeaRow.row_ref.in_(fmea_refs or ["_"])))).scalars():
        row.review_state = ReviewState.PENDING_RE_REVIEW
    for c in (await session.execute(
            select(ControlPlanItem).where(
                ControlPlanItem.control_ref.in_(control_refs or ["_"])))).scalars():
        c.review_state = ReviewState.PENDING_RE_REVIEW
    for p in (await session.execute(
            select(DesignParameter).where(
                DesignParameter.code.in_(param_refs or ["_"])))).scalars():
        p.review_state = ReviewState.PENDING_RE_REVIEW
    await session.flush()


async def review_impact(session: AsyncSession, impact_id: int, *, state: ImpactState,
                        actor: str, note: str | None = None,
                        request_id: str | None = None) -> ECNImpact:
    """Record the outcome of one re-review and clear the record's flag."""
    impact = (await session.execute(
        select(ECNImpact).where(ECNImpact.id == impact_id))).scalar_one_or_none()
    if impact is None:
        raise ECNError(f"Impact {impact_id} not found.")

    impact.state = state
    impact.reviewed_by = actor
    impact.reviewed_at = utcnow()
    impact.review_note = note

    if state in (ImpactState.RE_REVIEWED, ImpactState.NOT_AFFECTED):
        if impact.object_type in (ImpactObjectType.DFMEA_ROW, ImpactObjectType.PFMEA_ROW):
            row = (await session.execute(
                select(FmeaRow).where(FmeaRow.row_ref == impact.object_ref))).scalar_one_or_none()
            if row:
                row.review_state = ReviewState.CURRENT
        elif impact.object_type == ImpactObjectType.CONTROL_PLAN_ITEM:
            c = (await session.execute(
                select(ControlPlanItem)
                .where(ControlPlanItem.control_ref == impact.object_ref))).scalar_one_or_none()
            if c:
                c.review_state = ReviewState.CURRENT
        elif impact.object_type == ImpactObjectType.DESIGN_PARAMETER:
            p = (await session.execute(
                select(DesignParameter)
                .where(DesignParameter.code == impact.object_ref))).scalar_one_or_none()
            if p:
                p.review_state = ReviewState.CURRENT
    elif state == ImpactState.IN_PROGRESS:
        pass

    session.add(AuditEvent(actor=actor, action="ecn.impact_reviewed",
                           object_type=impact.object_type, object_ref=impact.object_ref,
                           request_id=request_id, detail={"state": state, "note": note}))
    await session.commit()
    await session.refresh(impact)
    return impact


async def decide(session: AsyncSession, ecn: ECN, *, approve: bool, actor: str,
                 note: str | None = None, request_id: str | None = None) -> ECN:
    """Change-board decision.

    Approval is refused while re-reviews are outstanding. That is the point of
    the workflow: approving a change whose consequences have not been assessed
    is how a cell swap reaches the line with a stale control plan behind it.
    """
    if ecn.state not in (ECNState.DRAFT, ECNState.UNDER_REVIEW):
        raise ECNError(f"{ecn.ecn_number} is {ecn.state}; only DRAFT or UNDER_REVIEW "
                       "changes can be decided.")
    if approve:
        pending = (await session.execute(
            select(func.count()).select_from(ECNImpact).where(
                ECNImpact.ecn_id == ecn.id,
                ECNImpact.state.in_([ImpactState.PENDING_RE_REVIEW,
                                     ImpactState.IN_PROGRESS])))).scalar_one()
        if pending:
            raise ECNError(
                f"{pending} impacted record(s) are still awaiting re-review. Approving "
                f"{ecn.ecn_number} now would release a change whose effect on the FMEA, "
                "control plan and certificates has not been assessed."
            )
        ecn.state = ECNState.APPROVED
        ecn.approved_by = actor
        ecn.approved_at = utcnow()
    else:
        ecn.state = ECNState.REJECTED
        ecn.rejected_reason = note

    session.add(AuditEvent(actor=actor, action="ecn.decide", object_type="ECN",
                           object_ref=ecn.ecn_number, request_id=request_id,
                           detail={"decision": "approve" if approve else "reject",
                                   "note": note}))
    await session.commit()
    await session.refresh(ecn)
    return ecn


async def summarise(session: AsyncSession, ecn: ECN) -> dict[str, Any]:
    impacts = (await session.execute(
        select(ECNImpact).where(ECNImpact.ecn_id == ecn.id))).scalars().all()
    by_type: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for i in impacts:
        by_type[i.object_type] = by_type.get(i.object_type, 0) + 1
        by_state[i.state] = by_state.get(i.state, 0) + 1
    return {**by_type, **{f"state_{k}": v for k, v in by_state.items()},
            "total": len(impacts)}
