"""N5.5 deterministic cost optimisation: exact Q_order (ceiling / MOQ / pack multiple), price-break
selection, extended cost, FX conversion, ranking and tie-breaks, all in Decimal, on the pure engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.n5 import engine
from app.n5.models import N5BomIntake, N5BomLine, N5Offer, N5Quote, N5Supplier
from app.rules import normalize_mpn

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def intake(build: int = 10, target: int = 30, lines=None, base: str = "INR") -> N5BomIntake:
    lines = lines or [(1, "GRM32ER72A475KA88", "Murata", 2), (2, "SRP1270-220M", "Bourns", 1)]
    return N5BomIntake(
        intake_id="N5I-T", tenant_id="t", project_id="PRJ", prd_id="PSU", prd_ref="PRD-PSU@A", bom_ref="BOM-PSU@B",
        bom_payload_sha256=SHA, manifest_id="REL-1", approval_id="APV-1", n3_release_id="N3R-1", scope="schematic_pcb",
        design_revision="B", build_quantity=build, purpose="PROTOTYPE_BUILD", target_lead_time_days=target,
        base_currency=base, release_source_id="SRC-PRC", synthetic=False, locked_by="pan", locked_at=NOW,
        lines=[N5BomLine(line=n, mpn=normalize_mpn(m), mpn_as_released=m, manufacturer=mf, refdes=[f"X{n}"],
                         qty_per_unit=q, q_required=q * build) for n, m, mf, q in lines])


def supplier(sid: str, kind: str = "DISTRIBUTOR", mfrs=("Murata", "Bourns")) -> N5Supplier:
    return N5Supplier(supplier_id=sid, kind=kind, legal_name=sid, country="IN", registration_ids={"GSTIN": "x"},
                      channel_proofs=[{"manufacturer": m, "proof_type": "MANUFACTURER_AUTHORIZED_LIST",
                                       "reference": "https://example.test", "valid_until": "2030-01-01"} for m in mfrs],
                      tenant_id="t", status="APPROVED", version=2, submitted_by="pan", submitted_at=NOW,
                      reviewed_by="sq", reviewed_at=NOW, approved_until=(NOW + timedelta(days=365)).date(),
                      verified_channel_manufacturers=list(mfrs))


def quote(sid: str, offers: list[dict], qid: str = "Q1", valid_days: float = 30, ccy: str = "INR") -> N5Quote:
    return N5Quote(quote_id=qid, tenant_id="t", bom_intake_id="N5I-T", supplier_id=sid, channel="MANUAL",
                   source_id="SRC-QUOTES", quote_ref=qid, design_revision="B", issued_at=NOW - timedelta(days=1),
                   valid_until=NOW + timedelta(days=valid_days), status="ACCEPTED", submitted_by="pan",
                   submitted_at=NOW, quote_sha256=SHA,
                   offers=[N5Offer.model_validate({"currency": ccy, "lead_time_days": 5, "stock_qty": 100_000,
                                                   "availability": "STOCK", **o, "offer_id": f"{qid}-O{i}",
                                                   "quote_id": qid}) for i, o in enumerate(offers)])


def o(line: int, mpn: str, breaks, moq: int = 1, pack: int = 1, charge: str = "12", **kw) -> dict:
    return {"line": line, "quoted_mpn": mpn, "moq": moq, "pack_multiple": pack,
            "price_breaks": [{"min_qty": q, "unit_price": p} for q, p in breaks],
            "line_charges": [{"kind": "HANDLING", "amount": charge}] if charge else [], **kw}


def run(quotes, i=None, sups=None, fx=None):
    i = i or intake()
    ctx = engine.Context(intake=i, now=NOW, suppliers=sups or {s: supplier(s) for s in {q.supplier_id for q in quotes}},
                         fx=fx or {}, source_status=lambda _: "APPROVED")
    return engine.assess(ctx, quotes, None)


M, B = "GRM32ER72A475KA88", "SRP1270-220M"


@pytest.mark.parametrize("moq, pack, q_order, ext", [
    (1, 1, 20, "142.00"),            # Q_required 2 × 10 = 20
    (10, 10, 20, "142.00"),
    (25, 1, 25, "174.50"),           # MOQ dominates
    (1, 15, 30, "207.00"),           # ceil(20 / 15) × 15
    (25, 10, 30, "207.00"),          # ceil(25 / 10) × 10
    (1, 4000, 4000, "26012.00"),     # a full reel
])
def test_q_order_and_extended_cost(moq, pack, q_order, ext):
    out = run([quote("SUP-A", [o(1, M, [(1, "6.50")], moq=moq, pack=pack), o(2, B, [(1, "9")])])])
    top = out["lines"][0].offers[0]
    assert (top.q_order, str(top.extended_cost)) == (q_order, ext)
    assert top.q_order % pack == 0 and top.q_order >= max(20, moq)


@pytest.mark.parametrize("breaks, price", [
    ([(1, "7.00"), (20, "6.50"), (100, "6.00")], "6.50"),        # exactly on a break
    ([(1, "7.00"), (21, "6.50")], "7.00"),                       # one short of the next break
    ([(10, "6.80"), (100, "6.00")], "6.80"),
])
def test_price_break_is_the_largest_reached_at_q_order(breaks, price):
    out = run([quote("SUP-A", [o(1, M, breaks), o(2, B, [(1, "9")])])])
    assert str(out["lines"][0].offers[0].unit_price) == price


def test_no_applicable_break_makes_the_offer_ineligible():
    out = run([quote("SUP-A", [o(1, M, [(25, "5.00")]), o(2, B, [(1, "9")])])])     # MOQ 1, Q 20 < 25
    assert out["lines"][0].offers[0].codes == ["PRICE_BREAK_MISSING"] and out["lines"][0].status == "UNSOURCED"


def test_step_up_to_a_cheaper_break_is_advice_only():
    out = run([quote("SUP-A", [o(1, M, [(1, "7.00"), (25, "5.00")]), o(2, B, [(1, "9")])])])
    top = out["lines"][0].offers[0]
    assert (top.q_order, str(top.unit_price), str(top.extended_cost)) == (20, "7.00", "152.00")
    assert top.step_up == {"q_order": 25, "unit_price": "5.00", "extended_cost": "137.00", "saving": "15.00",
                           "applied": False}
    assert "STEP_UP_SAVES" in {f.code for f in out["findings"]} and out["decision"] == "PASS"


def test_totals_are_exact_decimals():
    out = run([quote("SUP-A", [o(1, M, [(1, "0.1")], charge="0.2"), o(2, B, [(1, "0.3")], charge=None)])])
    assert out["indicative_total"] == D("5.2") and isinstance(out["indicative_total"], D)     # 20 × 0.1 + 0.2 + 10 × 0.3
    assert out["indicative_unit_cost"] == D("0.5200")


def test_unit_cost_rounds_half_up_to_four_places():
    i = intake(build=3, lines=[(1, M, "Murata", 1)])
    out = run([quote("SUP-A", [o(1, M, [(1, "3.33335")], charge=None)])], i=i)
    assert out["indicative_total"] == D("10.00005") and out["indicative_unit_cost"] == D("3.3334")


def test_ranking_and_deterministic_tie_breaks():
    q1 = quote("SUP-B", [o(1, M, [(1, "6.50")], lead_time_days=9)], qid="Q1")
    q2 = quote("SUP-A", [o(1, M, [(1, "6.50")], lead_time_days=9)], qid="Q2")
    q3 = quote("SUP-C", [o(1, M, [(1, "6.50")], lead_time_days=3)], qid="Q3")
    q4 = quote("SUP-D", [o(1, M, [(1, "6.40")], lead_time_days=20)], qid="Q4")
    out = run([q1, q2, q3, q4], i=intake(lines=[(1, M, "Murata", 2)]))
    assert [(x.supplier_id, x.rank) for x in out["lines"][0].offers] == [
        ("SUP-D", 1), ("SUP-C", 2), ("SUP-A", 3), ("SUP-B", 4)]       # cost, then lead time, then supplier id


def test_fx_conversion_is_exact_and_recorded():
    from app.models import FxAssumption
    fx = {("USD", "INR"): FxAssumption(fx_id="FX-1", from_currency="USD", to_currency="INR", rate=D("83.4567"),
                                       as_of=NOW.date(), source="treasury (test)")}
    out = run([quote("SUP-A", [o(1, M, [(1, "0.0779")], charge=None, currency="USD"), o(2, B, [(1, "9")])])], fx=fx)
    top = out["lines"][0].offers[0]
    assert top.extended_cost == D("1.5580") and top.extended_cost_base == D("1.5580") * D("83.4567")
    assert (top.fx_id, top.fx_rate) == ("FX-1", D("83.4567"))


def test_quantities_scale_with_build_quantity():
    out = run([quote("SUP-A", [o(1, M, [(1, "6.50"), (500, "5.90")], moq=10, pack=10), o(2, B, [(1, "9")])])],
              i=intake(build=250))
    top = out["lines"][0].offers[0]
    assert (top.q_order, str(top.unit_price), str(top.extended_cost)) == (500, "5.90", "2962.00")
