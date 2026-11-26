"""Revision cascade: a new N3 BOM revision (or N2 PRD revision) supersedes the N5 intake, marks its
quotes and reports STALE, blocks its N6 / N7 handoffs and the drafts downstream, and makes quotes for
the old revision ineligible (DESIGN_REVISION_OBSOLETE) against the new intake."""
from __future__ import annotations

from n5_support import BOURNS, MURATA, by_mpn, codes, offer


def test_new_bom_revision_cascades_to_quotes_reports_handoffs_and_drafts(flow5):
    intake, rep, h = flow5.handed_off()
    pk = {x["target"]: x for x in h["packages"]}
    n6 = flow5.c.post("/v1/n6/intake", "co", json={"artifact_id": pk["N6"]["artifact_id"],
                                                   "revision": pk["N6"]["revision"]}).json()
    assert flow5.rerelease("C").status_code == 201
    st = lambda kind, sid: flow5.c.get(f"/v1/n5/subjects/{kind}/{sid}/status", "pan").json()  # noqa: E731
    assert st("intake", intake["intake_id"])["status"] == "SUPERSEDED"
    assert st("report", rep["report_id"])["status"] == "STALE"
    q = flow5.c.get(f"/v1/n5/bom-intakes/{intake['intake_id']}", "pan").json()["quotes"][0]["quote_id"]
    assert st("quote", q)["status"] == "STALE"
    hs = st("handoff", h["handoff_id"])
    assert hs["status"] == "BLOCKED" and hs["history"][-1]["code"] == "DESIGN_REVISION_CHANGED"
    assert flow5.c.get(f"/v1/n6/bid-inputs/{n6['intake_id']}", "co").json()["status"] == "BLOCKED"
    r = flow5.c.post("/v1/n7/intake", "fo", json={"artifact_id": pk["N7"]["artifact_id"],
                                                  "revision": pk["N7"]["revision"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "HANDOFF_BLOCKED"


def test_stale_report_can_no_longer_be_reviewed_or_released(flow5):
    intake, rep = flow5.sourced()
    assert flow5.rerelease("C").status_code == 201
    r = flow5.n5_review(rep)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "REPORT_STALE"
    again = flow5.n5_report(intake)
    assert again["decision"] == "BLOCK" and "BOM_INTAKE_STALE" in codes(again, "BLOCK", "REPORT")


def test_old_revision_quotes_are_obsolete_on_the_new_intake(flow5):
    old_intake, rep = flow5.sourced()
    assert flow5.rerelease("C").status_code == 201
    flow5.load_lines()
    new = flow5.n5_intake()
    assert new["design_revision"] == "C" and new["intake_id"] != old_intake["intake_id"]
    rep2 = flow5.n5_report(new, quote_ids=rep["quote_ids"])
    assert all(x["codes"] == ["DESIGN_REVISION_OBSOLETE"] for ln in rep2["lines"] for x in ln["offers"])
    flow5.quote(new, "SUP-DIST-A", [offer(flow5.L[MURATA], MURATA, "6.50"), offer(flow5.L[BOURNS], BOURNS, "9")],
                rev="C")
    rep3 = flow5.n5_report(new)
    assert rep3["decision"] == "PASS" and rep3["indicative_total"] == "244.00"
    assert by_mpn(rep3, MURATA)["offers"][0]["quote_id"] not in rep["quote_ids"]


def test_a_supplier_quoting_the_wrong_revision_is_obsolete(flow5):
    flow5.bom_released()
    flow5.standard_suppliers()
    intake = flow5.n5_intake()
    flow5.quote(intake, "SUP-DIST-A", [offer(flow5.L[MURATA], MURATA, "6.50"), offer(flow5.L[BOURNS], BOURNS, "9")],
                rev="A")
    rep = flow5.n5_report(intake)
    assert rep["decision"] == "REWORK" and all("DESIGN_REVISION_OBSOLETE" in x["codes"]
                                               for ln in rep["lines"] for x in ln["offers"])


def test_prd_revision_supersedes_the_intake(flow5):
    intake, rep = flow5.sourced()
    body = flow5.prd_body(scopes=["schematic_pcb"])
    body["hazards"] = []
    body["product_name"] = "PSU-120W DIN-rail 48 V → 24 V (revision B)"
    assert flow5.c.post("/v1/n2/prds", "pm", json=body).status_code == 200
    assert flow5.c.post("/v1/n2/prds/PSU-120W/revisions/B/release", "pa",
                        json={"reason": "revised product name"}).status_code == 200
    st = flow5.c.get(f"/v1/n5/subjects/intake/{intake['intake_id']}/status", "pan").json()
    assert st["status"] == "SUPERSEDED" and "PRD" in st["history"][-1]["reason"]


def test_lazy_check_catches_a_new_revision_without_the_listener(flow5):
    intake, rep = flow5.sourced()
    n3 = flow5.c.app.state.n3
    n5 = flow5.c.app.state.n5
    n3.release_listeners.remove(n5.on_n3_release)
    assert flow5.rerelease("C").status_code == 201
    view = flow5.c.get(f"/v1/n5/bom-intakes/{intake['intake_id']}", "pan").json()
    assert view["status"] == "SUPERSEDED" and "BOM-PSU-120W-schematic_pcb@C" in view["reason"]
