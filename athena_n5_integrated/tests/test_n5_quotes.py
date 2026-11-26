"""N5 baseline (part 2 of 2): quotation intake (manual, signed EMS / ODM drop zone, CSV and X12 843),
FX assumptions, ECO proposals for alternates, and the licensing posture of distributor data."""
from __future__ import annotations

import json

import pytest

from n5_support import BOURNS, MURATA, by_mpn, codes, iso, days, offer


@pytest.fixture
def ready(flow5):
    flow5.bom_released()
    flow5.standard_suppliers()
    return flow5, flow5.n5_intake()


# ----------------------------------------------------------------------------------------------
# manual quotes: structural checks reject at intake, guardrails come later
# ----------------------------------------------------------------------------------------------
def test_manual_quote_is_accepted_hashed_and_scoped(ready):
    flow, intake = ready
    r = flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], MURATA, "6.50")])
    assert r.status_code == 201, r.text
    q = r.json()
    assert q["status"] == "ACCEPTED" and len(q["quote_sha256"]) == 64 and q["channel"] == "MANUAL"
    assert q["offers"][0]["offer_id"] == f"{q['quote_id']}-O1"
    assert q["data_use"] == {"training_allowed": False, "redistribution": "INTERNAL_PROJECT_ONLY",
                             "retention": "PROJECT_DECISION_RECORD"}


@pytest.mark.parametrize("change, code", [
    ({"breaks": [{"min_qty": 100, "unit_price": "6"}, {"min_qty": 10, "unit_price": "6.5"}]}, "PRICE_BREAKS_INVALID"),
    ({"breaks": [{"min_qty": 10, "unit_price": "6"}, {"min_qty": 10, "unit_price": "5"}]}, "PRICE_BREAKS_INVALID"),
    ({"uom": "REEL"}, "UOM_MISMATCH"),
    ({"stock": None}, "STOCK_MISSING"),
])
def test_structurally_invalid_offers_reject_the_quote(ready, change, code):
    flow, intake = ready
    r = flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], MURATA, "6.50", **change)])
    assert r.status_code == 422 and code in r.json()["rejection_codes"], r.text


def test_validity_window_must_be_positive(ready):
    flow, intake = ready
    r = flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], MURATA, "6.50")], valid=-2)
    assert r.status_code == 422 and "VALIDITY_INVALID" in r.json()["rejection_codes"]


def test_quotes_need_a_sourcing_source_and_a_registered_supplier(ready):
    flow, intake = ready
    r = flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], MURATA, "6.50")], source="SRC-PRC")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "QUOTE_SOURCE_PURPOSE"
    r = flow.quote(intake, "SUP-NOBODY", [offer(flow.L[MURATA], MURATA, "6.50")])
    assert r.status_code == 404 and r.json()["detail"]["code"] == "SUPPLIER_NOT_FOUND"


def test_rejected_quotes_are_kept_but_never_assessed(ready):
    flow, intake = ready
    flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], MURATA, "1.00", uom="REEL")])
    view = flow.c.get(f"/v1/n5/bom-intakes/{intake['intake_id']}", "pan").json()
    assert [q["status"] for q in view["quotes"]] == ["REJECTED"]
    rep = flow.n5_report(intake)
    assert rep["quote_ids"] == [] and rep["decision"] == "REWORK"


# ----------------------------------------------------------------------------------------------
# EMS / ODM signed drop zone
# ----------------------------------------------------------------------------------------------
def test_signed_ems_quote_is_verified_and_vaulted(ready):
    flow, intake = ready
    flow.ems_supplier()
    r = flow.ems_quote(intake)
    assert r.status_code == 201, r.text
    q = r.json()
    assert q["signature"]["verified"] and {f["role"] for f in q["files"]} == {"QUOTE_DOCUMENT", "CAPACITY_EVIDENCE"}
    assert q["nre"][0]["kind"] == "NRE" and q["offers"][0]["capacity"]["allocated_qty"] == 50


def test_tampered_ems_document_is_rejected(ready):
    flow, intake = ready
    flow.ems_supplier()
    r = flow.ems_quote(intake, tamper=True)
    assert r.status_code == 422 and {"SHA256_MISMATCH", "SIGNATURE_INVALID"} <= set(r.json()["rejection_codes"])


def test_unregistered_signing_key_is_rejected(ready):
    flow, intake = ready
    flow.ems_supplier()
    r = flow.ems_quote(intake, key_id="EMS-KEY-9")
    assert r.status_code == 422 and "SIGNATURE_INVALID" in r.json()["rejection_codes"]


def test_ems_quote_from_x12_843(ready):
    flow, intake = ready
    flow.ems_supplier()
    m, b = flow.L[MURATA], flow.L[BOURNS]
    edi = ("ISA*00*          *00*          *ZZ*EMS1           *ZZ*ACME           *261120*0900*U*00401*000000001*0*P*>~"
           "GS*RR*EMS1*ACME*20261120*0900*1*X*004010~ST*843*0001~BQT*00*EMS-Q-1*20261120~CUR*SE*INR~"
           "DTM*036*20301231~"
           f"PO1*{m}*20*EA*7.10*PE*MG*{MURATA}~CTP**PRP*7.10*1~CTP**PRP*6.90*100~PO4*10~QTY*MO*10~LDT*AE*12*DA~"
           f"PO1*{b}*10*EA*9.90*PE*MG*{BOURNS}~QTY*33*400~LDT*AE*3*DA~SE*14*0001~GE*1*1~IEA*1*000000001~").encode()
    r = flow.ems_quote(intake, offers=[], edi=edi)
    assert r.status_code == 201, r.text
    offers = {o["quoted_mpn"]: o for o in r.json()["offers"]}
    assert offers[MURATA]["price_breaks"] == [{"min_qty": 1, "unit_price": "7.10"}, {"min_qty": 100, "unit_price": "6.90"}]
    assert (offers[MURATA]["pack_multiple"], offers[MURATA]["moq"], offers[MURATA]["availability"]) == \
        (10, 10, "FACTORY_ORDER")
    assert (offers[BOURNS]["availability"], offers[BOURNS]["stock_qty"]) == ("STOCK", 400)


def test_undeclared_files_are_rejected(ready):
    flow, intake = ready
    flow.ems_supplier()
    doc = b"%PDF-1.4\n%%EOF\n"
    manifest = {"format": "athena-ems-quote/1", "supplier_id": "SUP-EMS-1", "bom_intake_id": intake["intake_id"],
                "design_revision": "B", "quote_ref": "EMS-Q-2", "issued_at": iso(days(-1)), "valid_until": iso(days(9)),
                "quote_document": "q.pdf", "quote_document_sha256": __import__("hashlib").sha256(doc).hexdigest(),
                "signature": {"key_id": "EMS-KEY-1", "algorithm": "ED25519",
                              "value": __import__("n5_support").sign(doc)},
                "offers": [offer(flow.L[MURATA], MURATA, "7")]}
    r = flow.c.post("/v1/n5/quotes/ems", "pan", data={"manifest": json.dumps(manifest), "source_id": "SRC-EMS"},
                    files=[("files", ("q.pdf", doc, "application/pdf")), ("files", ("extra.txt", b"x", "text/plain"))])
    assert r.status_code == 422 and "FILE_UNDECLARED" in r.json()["rejection_codes"]


# ----------------------------------------------------------------------------------------------
# FX assumptions
# ----------------------------------------------------------------------------------------------
def test_usd_offer_is_converted_with_the_recorded_fx_assumption(ready):
    flow, intake = ready
    assert flow.fx().status_code == 201
    m, b = flow.L[MURATA], flow.L[BOURNS]
    flow.quote(intake, "SUP-DIST-A", [offer(m, MURATA, "0.07", ccy="USD", charge=None),
                                      offer(b, BOURNS, "9")])
    rep = flow.n5_report(intake)
    top = by_mpn(rep, MURATA)["offers"][0]
    assert (top["extended_cost"], top["fx_rate"], top["extended_cost_base"]) == ("1.40", "83.50", "116.9000")
    assert rep["indicative_total"] == "218.9000" and [x["fx_id"] for x in rep["fx_assumptions"]] == ["FX-USD-INR"]


def test_offer_without_fx_assumption_is_ineligible(ready):
    flow, intake = ready
    flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], MURATA, "0.07", ccy="USD")])
    rep = flow.n5_report(intake)
    assert "FX_ASSUMPTION_MISSING" in by_mpn(rep, MURATA)["offers"][0]["codes"]


def test_fx_ids_are_immutable(ready):
    flow, intake = ready
    assert flow.fx().status_code == 201
    assert flow.fx(rate="90").status_code == 409


# ----------------------------------------------------------------------------------------------
# ECO proposals: an alternate is routed to N3, never substituted
# ----------------------------------------------------------------------------------------------
def _eco(flow, intake, **over):
    body = {"bom_intake_id": intake["intake_id"], "line": flow.L[MURATA], "proposed_mpn": "CL32B475KCJSNNE",
            "proposed_manufacturer": "Samsung Electro-Mechanics",
            "reason": "Murata lead time; Samsung X7R 1210 equivalent",
            "equivalence_evidence": [{"kind": "DATASHEET", "reference": "CL32B475KCJSNNE datasheet rev 3",
                                      "summary": "4.7 µF 100 V X7R 1210"}],
            "parametric_comparison": [{"parameter": "rated_voltage", "approved_value": "100", "proposed_value": "100",
                                       "unit": "V"}], **over}
    return flow.c.post("/v1/n5/eco-proposals", "pan", json=body)


def test_eco_proposal_is_pending_n3_and_changes_nothing(ready):
    flow, intake = ready
    r = _eco(flow, intake)
    assert r.status_code == 201 and r.json()["status"] == "PENDING_N3_ECO"
    assert r.json()["approved_mpn"] == MURATA and "never substitutes" in r.json()["note"]
    again = flow.c.get(f"/v1/n5/bom-intakes/{intake['intake_id']}", "pan").json()["intake"]
    assert {ln["mpn"] for ln in again["lines"]} == {MURATA, BOURNS}
    assert [e["eco_id"] for e in flow.c.get("/v1/n5/eco-proposals", "de").json()] == [r.json()["eco_id"]]


def test_eco_needs_evidence_and_a_real_alternate(ready):
    flow, intake = ready
    assert _eco(flow, intake, equivalence_evidence=[]).status_code == 422
    assert _eco(flow, intake, proposed_mpn=MURATA.lower()).json()["detail"]["code"] == "NOT_AN_ALTERNATE"


def test_alternate_mpn_offer_is_blocked_and_flagged_as_eco_candidate(ready):
    flow, intake = ready
    flow.quote(intake, "SUP-DIST-A", [offer(flow.L[MURATA], "CL32B475KCJSNNE", "3.00"),
                                      offer(flow.L[BOURNS], BOURNS, "9")])
    rep = flow.n5_report(intake)
    assert {"MPN_MISMATCH", "ECO_CANDIDATE"} <= codes(rep, level="OFFER")
    assert by_mpn(rep, MURATA)["status"] == "UNSOURCED" and rep["decision"] == "REWORK"


# ----------------------------------------------------------------------------------------------
# distributor APIs are off by default
# ----------------------------------------------------------------------------------------------
def test_live_distributor_apis_are_disabled_by_default(ready):
    flow, intake = ready
    r = flow.c.post(f"/v1/n5/bom-intakes/{intake['intake_id']}/lookups", "pan", json={"connectors": ["DIGIKEY"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LIVE_APIS_DISABLED"
    calls = flow.c.get("/v1/n5/connector-calls", "aud").json()
    assert calls[-1]["outcome"] == "DISABLED"
    status = flow.c.get("/v1/n5/status", "pan").json()
    assert status["live_apis_enabled"] is False and not status["connectors"]["DIGIKEY"]["persist_raw"]
    assert status["connectors"]["DIGIKEY"]["training_allowed"] is False


def test_ems_quote_from_a_signed_csv_document(ready):
    flow, intake = ready
    flow.ems_supplier()
    m, b = flow.L[MURATA], flow.L[BOURNS]
    doc = ("line,quoted_mpn,manufacturer,currency,moq,pack_multiple,availability,stock_qty,lead_time_days,price_breaks\n"
           f"{m},{MURATA},Murata,INR,10,10,STOCK,800,4,1:7.20;100:6.95\n"
           f"{b},{BOURNS},Bourns,INR,1,1,STOCK,300,4,1:9.80\n").encode()
    import hashlib
    from n5_support import sign
    manifest = {"format": "athena-ems-quote/1", "supplier_id": "SUP-EMS-1", "bom_intake_id": intake["intake_id"],
                "design_revision": "B", "quote_ref": "EMS-Q-CSV", "issued_at": iso(days(-1)),
                "valid_until": iso(days(9)), "quote_document": "quote.csv",
                "quote_document_sha256": hashlib.sha256(doc).hexdigest(),
                "signature": {"key_id": "EMS-KEY-1", "algorithm": "ED25519", "value": sign(doc)}}
    r = flow.c.post("/v1/n5/quotes/ems", "pan", data={"manifest": json.dumps(manifest), "source_id": "SRC-EMS"},
                    files=[("files", ("quote.csv", doc, "text/csv"))])
    assert r.status_code == 201, r.text
    o = {x["quoted_mpn"]: x for x in r.json()["offers"]}
    assert o[MURATA]["price_breaks"][1] == {"min_qty": 100, "unit_price": "6.95"} and o[BOURNS]["stock_qty"] == 300
