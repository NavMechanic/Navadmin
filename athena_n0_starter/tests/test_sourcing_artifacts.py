"""N0 release policy for N5 sourcing handoffs (SOURCING_HANDOFF → N6 / N7): the shared order-quantity,
price-break and extended-cost arithmetic, exact-MPN checks against the pinned N3 BOM, the
indicative-material label, and source-rights revocation (listeners and the release-time re-check)."""
from __future__ import annotations

import copy
from decimal import Decimal as D

import pytest

from app.models import EXCLUDED_COST_CATEGORIES, INDICATIVE_MATERIAL_LABEL
from app.rules import (applicable_break, canonical_json, extended_cost, normalize_mpn, order_quantity, sha256_hex,
                       unit_cost)
from app.scanning import sniff_mime, validate_upload
from conftest import codes
from test_design_artifacts import _art, _bom

SHA = "d" * 64
FUTURE = "2030-01-01T00:00:00Z"


@pytest.mark.parametrize("q_req, moq, pack, q_order", [
    (20, 1, 1, 20), (20, 25, 1, 25), (20, 10, 10, 20), (21, 10, 10, 30), (20, 25, 10, 30),
    (1, 1, 2500, 2500), (2500, 100, 2500, 2500), (2501, 1, 2500, 5000), (7, 3, 4, 8), (10, 10, 3, 12),
])
def test_order_quantity_is_ceiling_of_max_over_pack_times_pack(q_req, moq, pack, q_order):
    assert order_quantity(q_req, moq, pack) == q_order
    assert q_order % pack == 0 and q_order >= max(q_req, moq) and q_order - pack < max(q_req, moq)


@pytest.mark.parametrize("bad", [(0, 1, 1), (1, 0, 1), (1, 1, 0), (-5, 1, 1)])
def test_order_quantity_rejects_non_positive_inputs(bad):
    with pytest.raises(ValueError):
        order_quantity(*bad)


@pytest.mark.parametrize("q, price", [(9, "9.00"), (10, "8.50"), (99, "8.50"), (100, "7.00"), (5000, "7.00")])
def test_applicable_price_break_is_the_largest_reached(q, price):
    breaks = [(1, D("9.00")), (10, D("8.50")), (100, D("7.00"))]
    assert applicable_break(breaks, q)[1] == D(price)


def test_no_break_applies_below_the_first_minimum():
    assert applicable_break([(25, D("1.00"))], 20) is None


def test_demo_arithmetic_is_exact_decimal():
    c1 = extended_cost(order_quantity(20, 1, 1), D("6.50"), [D("12")])
    c2 = extended_cost(order_quantity(10, 1, 1), D("9"), [D("12")])
    assert (c1, c2, c1 + c2) == (D("142.00"), D("102"), D("244.00"))
    assert unit_cost(c1 + c2, 10) == D("24.4000")
    assert extended_cost(3, D("0.1"), [D("0.2")]) == D("0.5")              # no binary floating point drift


def test_mpn_normalisation_keeps_suffixes_significant():
    assert normalize_mpn(" rc0603fr-07 10kl ") == "RC0603FR-0710KL"
    assert normalize_mpn("LM5146RGYR") != normalize_mpn("LM5146RGYT")
    assert normalize_mpn("TPS54360DDAR") != normalize_mpn("TPS54360DDA")


# ----------------------------------------------------------------------------------------------
@pytest.fixture
def src(n0):
    n0.source("SRC-ENG", permitted_use=("SOURCING", "COMMERCIAL", "FINANCE", "VERIFICATION"))
    n0.source("SRC-QUOTES", permitted_use=("SOURCING",))
    bom = _art("BOM-1", "BOM_RELEASE", _bom(), "SOURCING")
    bom["revision"] = "B"
    assert n0.evaluate(bom)["decision"]["decision"] == "PASS"
    return n0


def _line(**over) -> dict:
    ln = {"line": 1, "mpn": "RC0603FR-0710KL", "manufacturer": "Yageo", "qty_per_unit": 2, "q_required": 20,
          "critical": False, "supplier_id": "SUP-DIST-1", "supplier_kind": "DISTRIBUTOR",
          "supplier_qualification": "APPROVED", "quote_id": "Q-1", "quote_sha256": SHA, "quote_source_id": "SRC-QUOTES",
          "quoted_mpn": "RC0603FR-0710KL", "moq": 10, "pack_multiple": 10, "q_order": 20,
          "price_breaks": [{"min_qty": 1, "unit_price": "7.00"}, {"min_qty": 10, "unit_price": "6.50"}],
          "unit_price": "6.50", "currency": "INR", "line_charges": [{"kind": "HANDLING", "amount": "12"}],
          "extended_cost": "142.00", "extended_cost_base": "142.00", "availability": "STOCK", "stock_qty": 5000,
          "lead_time_days": 3, "valid_until": FUTURE, "eligible_offers": 2}
    ln.update(over)
    return ln


def _line2(**over) -> dict:
    return _line(**{"line": 2, "mpn": "GRM188R71H104KA93D", "manufacturer": "Murata", "qty_per_unit": 1,
                    "q_required": 10, "quote_id": "Q-2", "quoted_mpn": "GRM188R71H104KA93D", "moq": 1,
                    "pack_multiple": 1, "q_order": 10, "price_breaks": [{"min_qty": 1, "unit_price": "9"}],
                    "unit_price": "9", "extended_cost": "102", "extended_cost_base": "102", **over})


def _handoff(lines=None, **over) -> dict:
    lines = [_line(), _line2()] if lines is None else lines
    p = {"handoff_id": "N5H-1", "target": "N7", "prd_ref": "PRD-1@A", "bom_ref": "BOM-1@B", "design_revision": "B",
         "pcb_revision": "B", "build_quantity": 10, "report_id": "N5R-1", "report_sha256": SHA, "quote_hashes": [SHA],
         "review_id": "N5V-1", "approved_by": "pa", "submitters": ["an"], "quote_source_ids": ["SRC-QUOTES"],
         "base_currency": "INR", "lines": lines, "indicative_total": "244.00", "indicative_unit_cost": "24.4000",
         "indicative_scope": INDICATIVE_MATERIAL_LABEL, "excluded_cost_categories": list(EXCLUDED_COST_CATEGORIES),
         "approval_scope": "indicative material quote only"}
    p.update(over)
    return p


def _hart(payload: dict, purpose: str = "FINANCE", **over) -> dict:
    a = {**_art("PRC-1", "SOURCING_HANDOFF", payload, purpose), "source_artifact_ids": ["BOM-1@B"]}
    a.update(over)
    a["source_sha256"] = sha256_hex(canonical_json(a["payload"]))
    return a


@pytest.mark.parametrize("purpose, dest", [("COMMERCIAL", ["N6", "N7"]), ("FINANCE", ["N7"])])
def test_sourcing_handoff_passes_and_routes_to_n6_n7(src, purpose, dest):
    res = src.evaluate(_hart(_handoff(), purpose))
    assert res["decision"]["decision"] == "PASS", res["decision"]["explanation"]
    assert res["decision"]["allowed_destinations"] == dest


def test_foreign_currency_needs_a_matching_fx_assumption(src):
    fx = {"fx_id": "FX-1", "from_currency": "USD", "to_currency": "INR", "rate": "83.50", "as_of": "2026-11-20",
          "source": "treasury reference rate (synthetic)"}
    usd = _line2(currency="USD", price_breaks=[{"min_qty": 1, "unit_price": "0.11"}], unit_price="0.11",
                 line_charges=[], extended_cost="1.10", fx_id="FX-1", fx_rate="83.50", extended_cost_base="91.8500")
    ok = src.evaluate(_hart(_handoff([_line(), usd], fx_assumptions=[fx], indicative_total="233.8500",
                                     indicative_unit_cost="23.3850")))
    assert ok["decision"]["decision"] == "PASS", ok["decision"]["explanation"]
    bad = src.evaluate(_hart(_handoff([_line(), {**usd, "fx_id": None, "fx_rate": None}], indicative_total="233.8500",
                                      indicative_unit_cost="23.3850"), artifact_id="PRC-2"))
    assert bad["decision"]["decision"] == "BLOCK" and "FX_ASSUMPTION_MISSING" in codes(bad)


@pytest.mark.parametrize("change, code", [
    ({"lines": [_line(q_order=25), _line2()]}, "ORDER_QUANTITY_MISMATCH"),               # ceil(20/10)·10 = 20
    ({"lines": [_line(q_required=18), _line2()]}, "ORDER_QUANTITY_MISMATCH"),            # 2 × 10 = 20
    ({"lines": [_line(unit_price="7.00", extended_cost="152.00", extended_cost_base="152.00"), _line2()],
      "indicative_total": "254.00", "indicative_unit_cost": "25.4000"}, "PRICE_BREAK_MISMATCH"),
    ({"lines": [_line(extended_cost="130.00", extended_cost_base="130.00"), _line2()],
      "indicative_total": "232.00", "indicative_unit_cost": "23.2000"}, "EXTENDED_COST_MISMATCH"),
    ({"indicative_total": "240.00", "indicative_unit_cost": "24.0000"}, "EXTENDED_COST_MISMATCH"),
    ({"indicative_unit_cost": "24.00"}, "EXTENDED_COST_MISMATCH"),
    ({"lines": [_line(valid_until="2026-01-01T00:00:00Z"), _line2()]}, "QUOTE_EXPIRED"),
    ({"lines": [_line(quoted_mpn="RC0603FR-0710KL-TR"), _line2()]}, "MPN_NOT_EXACT"),
    ({"lines": [_line(mpn="RC0603JR-0710KL", quoted_mpn="RC0603JR-0710KL"), _line2()]}, "MPN_NOT_EXACT"),
    ({"lines": [_line(), _line2(qty_per_unit=2, q_required=20, q_order=20, extended_cost="192",
                                extended_cost_base="192")],
      "indicative_total": "334.00", "indicative_unit_cost": "33.4000"}, "MPN_NOT_EXACT"),
    ({"lines": [_line()], "indicative_total": "142.00", "indicative_unit_cost": "14.2000"}, "LINE_UNSOURCED"),
    ({"lines": [_line(supplier_qualification="PENDING"), _line2()]}, "SUPPLIER_NOT_APPROVED"),
    ({"indicative_scope": "Total product cost"}, "INDICATIVE_SCOPE_MISSING"),
    ({"excluded_cost_categories": ["GST_AND_TAXES"]}, "INDICATIVE_SCOPE_MISSING"),
    ({"quote_source_ids": ["SRC-QUOTES", "SRC-UNKNOWN"]}, "SOURCE_RIGHTS_REVOKED"),
    ({"approved_by": "an"}, "SEPARATION_OF_DUTIES"),
    ({"bom_ref": "BOM-OTHER@B"}, "BOM_REVISION_UNLOCKED"),
    ({"design_revision": "C"}, "BOM_REVISION_UNLOCKED"),
    ({"lines": [_line(fx_id="FX-X"), _line2()]}, "FX_ASSUMPTION_MISSING"),
])
def test_sourcing_hard_policies_block(src, change, code):
    res = src.evaluate(_hart(_handoff(**change)))
    assert res["decision"]["decision"] == "BLOCK" and code in codes(res), res["decision"]["explanation"]


def test_dnp_lines_need_no_offer(src):
    bom = _bom()
    bom["items"].append({"line": 3, "mpn": "BAV99", "manufacturer": "Nexperia", "refdes": ["D9"], "qty": 1,
                         "dnp": True})
    art = _art("BOM-2", "BOM_RELEASE", bom, "SOURCING")
    art["revision"] = "B"
    assert src.evaluate(art)["decision"]["decision"] == "PASS"
    res = src.evaluate(_hart(_handoff(bom_ref="BOM-2@B"), source_artifact_ids=["BOM-2@B"]))
    assert res["decision"]["decision"] == "PASS", res["decision"]["explanation"]


def _revoke(n0, source_id: str):
    return n0.post(f"/v1/sources/acme/{source_id}/approve", "rita",
                   json={"decision": "REVOKED", "reason": "distributor licence terminated"})


def test_revoking_a_quote_source_blocks_new_handoffs_and_notifies_listeners(src):
    seen = []
    src.c.app.state.source_rights_listeners.append(lambda *a: seen.append(a))
    assert _revoke(src, "SRC-QUOTES").status_code == 200
    assert seen == [("acme", "SRC-QUOTES", "REVOKED")]
    res = src.evaluate(_hart(_handoff()))
    assert res["decision"]["decision"] == "BLOCK" and "SOURCE_RIGHTS_REVOKED" in codes(res)


def test_release_is_refused_once_a_dependency_is_revoked(src):
    assert src.evaluate(_hart(_handoff()))["decision"]["decision"] == "PASS"
    assert src.approve_release("PRC-1").status_code == 200
    assert _revoke(src, "SRC-QUOTES").status_code == 200
    r = src.release("PRC-1", targets=("N7",))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SOURCE_RIGHTS_REVOKED"


def test_release_is_refused_when_the_artifacts_own_source_is_revoked(src):
    assert src.evaluate(_hart(_handoff()))["decision"]["decision"] == "PASS"
    assert src.approve_release("PRC-1").status_code == 200
    assert _revoke(src, "SRC-ENG").status_code == 200
    assert src.release("PRC-1", targets=("N7",)).json()["detail"]["code"] == "SOURCE_RIGHTS_REVOKED"


def test_a_failing_listener_never_undoes_the_revocation(src):
    def boom(*_):
        raise RuntimeError("listener down")
    src.c.app.state.source_rights_listeners.append(boom)
    assert _revoke(src, "SRC-QUOTES").status_code == 200
    assert src.get("/v1/sources/acme/SRC-QUOTES", "sam").json()["approval_status"] == "REVOKED"
    events = src.get("/v1/audit/events").json()
    assert any(e["event_type"] == "SOURCE_RIGHTS_LISTENER_FAILED" for e in events)


def test_handoff_payload_is_not_mutated_by_evaluation(src):
    p = _handoff()
    before = copy.deepcopy(p)
    src.evaluate(_hart(p))
    assert p == before


def test_edi_quotations_are_recognised_by_content():
    x12 = b"ISA*00*          *00*          *ZZ*EMS1*ZZ*ACME*261120*0900*U*00401*1*0*P*>~ST*843*0001~"
    assert sniff_mime(x12) == "application/edi-x12" and validate_upload(x12, "q.edi", "text/plain", 10 ** 6).ok
    assert sniff_mime(b"UNB+UNOC:3+EMS1+ACME+261120:0900+1'") == "application/edifact"
    assert not validate_upload(x12, "q.pdf", None, 10 ** 6).ok                      # extension must still match
