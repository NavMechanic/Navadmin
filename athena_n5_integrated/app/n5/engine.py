"""Deterministic N5 guardrails and cost optimisation. Pure functions: no I/O, no clock, no model.

Order of operations (never changed by price):

1. every offer is matched to its BOM line and checked against the hard rejection matrix;
2. offers with any BLOCK or REWORK finding are removed;
3. only the remaining (eligible) offers are costed and ranked by extended material cost.

    Q_order        = ceil(max(Q_required, MOQ) / pack multiple) × pack multiple
    unit price     = the price break with the largest minimum quantity ≤ Q_order
    extended cost  = Q_order × unit price + quoted line charges            (quote currency)
    base cost      = extended cost × FX assumption rate                    (base currency)

All arithmetic is exact ``Decimal`` / ``int``, shared with N0 (``app.rules``), which recomputes it at
release. A cheaper offer can never outrank a BLOCK / REWORK guardrail because it is filtered first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from ..repository import new_id
from ..rules import applicable_break, extended_cost, normalize_mpn, order_quantity, unit_cost
from .models import (FxAssumption, GuardrailFinding, LineResult, N5BomIntake, N5BomLine, N5Offer, N5Quote,
                     N5Supplier, RankedOffer)

ENGINE_VERSION = "n5-engine/1.0.0"

# code → (outcome, level, description)
RULES: dict[str, tuple[str, str, str]] = {
    # BLOCK: the offer is ineligible, whatever it costs
    "MPN_MISMATCH": ("BLOCK", "OFFER", "quoted MPN differs from the approved BOM MPN (route as an N3 ECO)"),
    "DESIGN_REVISION_OBSOLETE": ("BLOCK", "OFFER", "quotation linked to an obsolete design / BOM revision"),
    "SUPPLIER_NOT_APPROVED": ("BLOCK", "OFFER", "supplier lacks a current APPROVED qualification"),
    "UNAUTHORIZED_CHANNEL": ("BLOCK", "OFFER", "supplier is not an authorized channel for the BOM manufacturer"),
    "QUOTE_EXPIRED": ("BLOCK", "OFFER", "quotation has expired"),
    "SOURCE_RIGHTS_REVOKED": ("BLOCK", "OFFER", "N0 source rights of the quotation are no longer APPROVED"),
    "QUOTE_HASH_MISMATCH": ("BLOCK", "OFFER", "stored quotation files no longer match their SHA-256"),
    # REWORK: the offer is ineligible until the supplier or the plan changes
    "STOCK_INSUFFICIENT": ("REWORK", "OFFER", "stock is below the proposed order quantity"),
    "LEAD_TIME_EXCEEDS_TARGET": ("REWORK", "OFFER", "lead time exceeds the project target"),
    "CAPACITY_EVIDENCE_MISSING": ("REWORK", "OFFER", "factory / EMS capacity evidence missing or insufficient"),
    "FX_ASSUMPTION_MISSING": ("REWORK", "OFFER", "no FX assumption converts the quote currency to the base"),
    "PRICE_BREAK_MISSING": ("REWORK", "OFFER", "no price break applies at the order quantity"),
    # line and report level
    "CRITICAL_LINE_UNSOURCED": ("REWORK", "LINE", "a critical BOM line has no eligible quotation"),
    "LINE_UNSOURCED": ("REWORK", "LINE", "a BOM line has no eligible quotation"),
    "BOM_INTAKE_STALE": ("BLOCK", "REPORT", "the BOM intake is superseded or invalidated"),
    "DESIGN_NOT_VERIFIED": ("BLOCK", "REPORT", "PRODUCTION sourcing without an N4 VERIFIED release of this revision"),
    # information and risk flags (never change eligibility or the decision)
    "SINGLE_ELIGIBLE_SOURCE": ("INFO", "LINE", "only one eligible offer"),
    "LEAD_TIME_NEAR_TARGET": ("INFO", "OFFER", "lead time uses most of the project target"),
    "QUALIFICATION_EXPIRING": ("INFO", "OFFER", "supplier qualification expires soon"),
    "STEP_UP_SAVES": ("INFO", "OFFER", "ordering up to the next price break would cost less (advice only)"),
    "ECO_CANDIDATE": ("INFO", "OFFER", "an alternate MPN was quoted; raise an N3 ECO proposal with evidence"),
    "DESIGN_NOT_YET_VERIFIED": ("INFO", "REPORT", "no N4 VERIFIED release of this revision yet (prototype build)"),
    "EMS_TURNKEY_SOURCING": ("INFO", "OFFER", "EMS turnkey offer: channel traceability is the EMS's obligation"),
}
_SUFFIXES = {"inc", "incorporated", "ltd", "limited", "gmbh", "corp", "corporation", "co", "llc", "ag", "sa", "plc",
             "bv", "nv", "kk", "pvt", "private", "the"}


def normalize_manufacturer(name: Optional[str]) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return "".join(w for w in words if w not in _SUFFIXES)


class Findings:
    def __init__(self) -> None:
        self.items: list[GuardrailFinding] = []

    def add(self, code: str, message: str, *, line: Optional[int] = None, offer: Optional[N5Offer] = None,
            quote: Optional[N5Quote] = None) -> None:
        outcome, level, _ = RULES[code]
        self.items.append(GuardrailFinding(finding_id=new_id("N5F"), code=code, outcome=outcome, level=level,
                                           message=message, line=line, offer_id=offer.offer_id if offer else None,
                                           quote_id=quote.quote_id if quote else None,
                                           supplier_id=quote.supplier_id if quote else None))


def decide(findings: list[GuardrailFinding]) -> str:
    """Only line- and report-level findings decide; offer-level ones only remove offers."""
    outcomes = {f.outcome for f in findings if f.level in ("LINE", "REPORT")}
    return "BLOCK" if "BLOCK" in outcomes else "REWORK" if "REWORK" in outcomes else "PASS"


@dataclass
class Context:
    intake: N5BomIntake
    now: datetime
    suppliers: dict[str, N5Supplier]
    fx: dict[tuple[str, str], FxAssumption]
    source_status: Callable[[str], Optional[str]]
    tampered: dict[str, str] = field(default_factory=dict)       # quote_id → problem
    quote_bom_refs: dict[str, str] = field(default_factory=dict)  # quote_id → BOM ref of the intake it quoted
    lead_time_warn_ratio: Decimal = Decimal("0.8")
    qualification_warn_days: int = 30


# ----------------------------------------------------------------------------------------------
# supplier eligibility
# ----------------------------------------------------------------------------------------------
def supplier_problems(supplier: Optional[N5Supplier], line: N5BomLine, at: datetime) -> list[tuple[str, str]]:
    if supplier is None:
        return [("SUPPLIER_NOT_APPROVED", "supplier is not registered in N5")]
    if supplier.status != "APPROVED":
        return [("SUPPLIER_NOT_APPROVED", f"{supplier.supplier_id} qualification is {supplier.status}")]
    if supplier.approved_until is None or supplier.approved_until < at.date():
        return [("SUPPLIER_NOT_APPROVED", f"{supplier.supplier_id} qualification lapsed {supplier.approved_until}")]
    mfr = normalize_manufacturer(line.manufacturer)
    if supplier.kind == "DISTRIBUTOR":
        verified = {normalize_manufacturer(m) for m in supplier.verified_channel_manufacturers}
        proofs = [c for c in supplier.channel_proofs if normalize_manufacturer(c.manufacturer) == mfr
                  and normalize_manufacturer(c.manufacturer) in verified and c.valid_until >= at.date()]
        if not mfr or not proofs:
            return [("UNAUTHORIZED_CHANNEL", f"{supplier.supplier_id} has no verified, current authorized-channel proof "
                                             f"for {line.manufacturer or 'an unnamed manufacturer'}")]
    elif supplier.kind == "MANUFACTURER":
        if mfr not in {normalize_manufacturer(m) for m in supplier.manufacturer_names}:
            return [("UNAUTHORIZED_CHANNEL", f"{supplier.supplier_id} does not make {line.manufacturer}")]
    return []


# ----------------------------------------------------------------------------------------------
# costing one offer
# ----------------------------------------------------------------------------------------------
@dataclass
class Costed:
    q_order: int
    brk: Optional[tuple[int, Decimal]]
    charges: Decimal
    ext: Optional[Decimal]
    fx: Optional[FxAssumption]
    base: Optional[Decimal]
    step_up: Optional[dict]


def cost_offer(offer: N5Offer, line: N5BomLine, base_ccy: str,
               fx: dict[tuple[str, str], FxAssumption]) -> Costed:
    q = order_quantity(line.q_required, offer.moq, offer.pack_multiple)
    breaks = [(b.min_qty, b.unit_price) for b in offer.price_breaks]
    brk = applicable_break(breaks, q)
    charges = sum((c.amount for c in offer.line_charges), Decimal(0))
    ext = extended_cost(q, brk[1], [c.amount for c in offer.line_charges]) if brk else None
    a = None if offer.currency == base_ccy else fx.get((offer.currency, base_ccy))
    rate = Decimal(1) if offer.currency == base_ccy else (a.rate if a else None)
    base = ext * rate if ext is not None and rate is not None else None
    step = None
    if ext is not None:
        for mq, price in sorted(breaks):
            if mq <= q:
                continue
            q2 = order_quantity(mq, offer.moq, offer.pack_multiple)
            b2 = applicable_break(breaks, q2)
            c2 = extended_cost(q2, b2[1], [c.amount for c in offer.line_charges])
            if c2 < ext:
                step = {"q_order": q2, "unit_price": str(b2[1]), "extended_cost": str(c2),
                        "saving": str(ext - c2), "applied": False}
            break
    return Costed(q, brk, charges, ext, a, base, step)


def _check_offer(ctx: Context, quote: N5Quote, offer: N5Offer, line: N5BomLine,
                 f: Findings) -> tuple[list[str], Costed]:
    """Hard rejection matrix for one offer. Returns the BLOCK / REWORK codes (empty = eligible) and the
    costing, which is only used for ranking when the offer is eligible."""
    codes: list[str] = []

    def hit(code: str, msg: str) -> None:
        f.add(code, msg, line=line.line, offer=offer, quote=quote)
        if RULES[code][0] != "INFO":
            codes.append(code)

    intake = ctx.intake
    if normalize_mpn(offer.quoted_mpn) != line.mpn:
        hit("MPN_MISMATCH", f"line {line.line}: {quote.supplier_id} quoted {offer.quoted_mpn!r}, the approved BOM "
                            f"part is {line.mpn_as_released!r}")
        hit("ECO_CANDIDATE", f"{offer.quoted_mpn} may only be bought after an N3 ECO adds it to a released BOM")
    quoted_bom = ctx.quote_bom_refs.get(quote.quote_id, intake.bom_ref)
    if quoted_bom != intake.bom_ref or quote.design_revision != intake.design_revision:
        hit("DESIGN_REVISION_OBSOLETE", f"quote {quote.quote_ref} is for {quoted_bom} / design revision "
                                        f"{quote.design_revision}; the active BOM is {intake.bom_ref}")
    sup = ctx.suppliers.get(quote.supplier_id)
    for code, msg in supplier_problems(sup, line, ctx.now):
        hit(code, f"line {line.line}: {msg}")
    if sup is not None and sup.status == "APPROVED" and sup.approved_until is not None and \
            sup.approved_until - ctx.now.date() <= timedelta(days=ctx.qualification_warn_days):
        hit("QUALIFICATION_EXPIRING", f"{sup.supplier_id} qualification ends {sup.approved_until}")
    if quote.valid_until <= ctx.now:
        hit("QUOTE_EXPIRED", f"quote {quote.quote_ref} expired {quote.valid_until.isoformat()}")
    status = ctx.source_status(quote.source_id)
    if status != "APPROVED":
        hit("SOURCE_RIGHTS_REVOKED", f"source {quote.source_id} of quote {quote.quote_ref} is "
                                     f"{status or 'not registered'}")
    if quote.quote_id in ctx.tampered:
        hit("QUOTE_HASH_MISMATCH", ctx.tampered[quote.quote_id])
    c = cost_offer(offer, line, intake.base_currency, ctx.fx)
    if c.brk is None:
        hit("PRICE_BREAK_MISSING", f"no price break at Q_order {c.q_order} (first break "
                                   f"{min(b.min_qty for b in offer.price_breaks)})")
    if offer.currency != intake.base_currency and c.fx is None:
        hit("FX_ASSUMPTION_MISSING", f"no FX assumption {offer.currency} → {intake.base_currency}")
    if offer.availability == "STOCK" and (offer.stock_qty is None or offer.stock_qty < c.q_order):
        hit("STOCK_INSUFFICIENT", f"line {line.line}: stock {offer.stock_qty} < proposed order {c.q_order}")
    target = intake.target_lead_time_days
    if offer.lead_time_days > target:
        hit("LEAD_TIME_EXCEEDS_TARGET", f"line {line.line}: lead time {offer.lead_time_days} d > target {target} d")
    elif target and Decimal(offer.lead_time_days) >= ctx.lead_time_warn_ratio * target:
        hit("LEAD_TIME_NEAR_TARGET", f"lead time {offer.lead_time_days} d of a {target} d target")
    capacity_needed = None
    if sup is not None and sup.kind in ("EMS", "ODM"):
        capacity_needed = intake.build_quantity
        hit("EMS_TURNKEY_SOURCING", f"{sup.supplier_id} sources {line.mpn} as part of a turnkey EMS service")
    if offer.availability == "FACTORY_ORDER":
        capacity_needed = max(capacity_needed or 0, c.q_order)
    if capacity_needed is not None:
        cap = offer.capacity
        need_by = (ctx.now + timedelta(days=offer.lead_time_days)).date()
        if cap is None or cap.document_sha256 is None or cap.allocated_qty < capacity_needed or \
                cap.period_end < need_by or cap.period_start > need_by:
            hit("CAPACITY_EVIDENCE_MISSING", f"line {line.line}: needs signed capacity evidence for {capacity_needed} "
                                             f"units covering {need_by}; got "
                                             f"{cap.allocated_qty if cap else 'none'}")
    if c.step_up:
        hit("STEP_UP_SAVES", f"ordering {c.step_up['q_order']} would cost {c.step_up['extended_cost']} instead of "
                             f"{c.ext} (not applied)")
    return codes, c


def match_line(offer: N5Offer, by_line: dict[int, N5BomLine], by_mpn: dict[str, N5BomLine]) -> Optional[N5BomLine]:
    if offer.line is not None:
        return by_line.get(offer.line)
    return by_mpn.get(normalize_mpn(offer.quoted_mpn))


def assess(ctx: Context, quotes: list[N5Quote], intake_stale: Optional[str]) -> dict:
    """Returns findings, per-line results (eligible offers ranked first), totals and the decision."""
    f = Findings()
    intake = ctx.intake
    by_line = {ln.line: ln for ln in intake.lines}
    by_mpn = {ln.mpn: ln for ln in intake.lines}
    per_line: dict[int, list[RankedOffer]] = {ln.line: [] for ln in intake.lines}
    selected_quotes: dict[str, str] = {}
    excluded: list[dict] = []
    if intake_stale:
        f.add("BOM_INTAKE_STALE", intake_stale)
    if intake.purpose == "PRODUCTION" and intake.verification_status != "VERIFIED":
        f.add("DESIGN_NOT_VERIFIED", f"PRODUCTION sourcing needs an N4 VERIFIED release of design revision "
                                     f"{intake.design_revision}")
    elif intake.verification_status != "VERIFIED":
        f.add("DESIGN_NOT_YET_VERIFIED", f"design revision {intake.design_revision} has no N4 VERIFIED release yet")
    for q in quotes:
        for o in q.offers:
            line = match_line(o, by_line, by_mpn)
            if line is None:
                f.add("MPN_MISMATCH", f"{q.supplier_id} quoted {o.quoted_mpn!r}, which matches no line of "
                                      f"{intake.bom_ref}", offer=o, quote=q)
                continue
            codes, c = _check_offer(ctx, q, o, line, f)
            sup = ctx.suppliers.get(q.supplier_id)
            per_line[line.line].append(RankedOffer(
                offer_id=o.offer_id, quote_id=q.quote_id, quote_sha256=q.quote_sha256, supplier_id=q.supplier_id,
                supplier_kind=sup.kind if sup else None, quoted_mpn=o.quoted_mpn, eligible=not codes,
                codes=sorted(set(codes)), q_order=c.q_order, price_break_min_qty=c.brk[0] if c.brk else None,
                unit_price=c.brk[1] if c.brk else None, currency=o.currency, line_charges_total=c.charges,
                extended_cost=c.ext, fx_id=c.fx.fx_id if c.fx else None, fx_rate=c.fx.rate if c.fx else None,
                extended_cost_base=c.base, availability=o.availability, stock_qty=o.stock_qty,
                lead_time_days=o.lead_time_days, step_up=c.step_up))
    lines: list[LineResult] = []
    total = Decimal(0)
    complete = True
    for ln in intake.lines:
        offers = per_line[ln.line]
        eligible = sorted((x for x in offers if x.eligible),
                          key=lambda x: (x.extended_cost_base, x.lead_time_days, x.supplier_id, x.offer_id))
        ranked = [x.model_copy(update={"rank": i + 1}) for i, x in enumerate(eligible)]
        ineligible = sorted((x for x in offers if not x.eligible), key=lambda x: (x.supplier_id, x.offer_id))
        flags = []
        if not ranked:
            complete = False
            code = "CRITICAL_LINE_UNSOURCED" if ln.critical else "LINE_UNSOURCED"
            f.add(code, f"line {ln.line} ({ln.mpn_as_released}): no eligible quotation among {len(offers)} offer(s)",
                  line=ln.line)
        else:
            top = ranked[0]
            total += top.extended_cost_base
            selected_quotes[top.quote_id] = top.quote_sha256 or ""
            if len(ranked) == 1:
                f.add("SINGLE_ELIGIBLE_SOURCE", f"line {ln.line}: only {top.supplier_id} is eligible", line=ln.line)
                flags.append("SINGLE_ELIGIBLE_SOURCE")
            if ln.critical:
                flags.append("CRITICAL_LINE")
            if top.lead_time_days and Decimal(top.lead_time_days) >= ctx.lead_time_warn_ratio * \
                    intake.target_lead_time_days:
                flags.append("LEAD_TIME_NEAR_TARGET")
            if top.supplier_kind in ("EMS", "ODM"):
                flags.append("EMS_TURNKEY_SOURCING")
            sel = next(o for q in quotes for o in q.offers if o.offer_id == top.offer_id)
            for x in sel.excluded_charges:
                excluded.append({"line": ln.line, "quote_id": top.quote_id, **x.model_dump(mode="json"),
                                 "in_indicative_total": False})
        lines.append(LineResult(line=ln.line, mpn=ln.mpn, manufacturer=ln.manufacturer, qty_per_unit=ln.qty_per_unit,
                                q_required=ln.q_required, critical=ln.critical,
                                status="SOURCED" if ranked else "UNSOURCED",
                                selected_offer_id=ranked[0].offer_id if ranked else None,
                                offers=ranked + ineligible, risk_flags=flags))
    for q in quotes:
        if q.quote_id in selected_quotes:
            for x in q.nre:
                excluded.append({"line": None, "quote_id": q.quote_id, **x.model_dump(mode="json"),
                                 "in_indicative_total": False})
    return {"findings": f.items, "lines": lines, "decision": decide(f.items),
            "indicative_total": total if complete else None,
            "indicative_unit_cost": unit_cost(total, intake.build_quantity) if complete else None,
            "selected_quotes": selected_quotes, "excluded": excluded}
