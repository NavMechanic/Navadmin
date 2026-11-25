"""PFMEA / control plan / CAPA: human-rated entries, the custom attention heuristic (never claimed as
AIAG-VDA Action Priority), an optional licensed AP table, and the risk-based blocks."""
from __future__ import annotations

import json

import pytest

from app.n4 import HEURISTIC_LABEL
from app.n4.engine import ApTable, attention, rate_entry
from app.n4.models import PfmeaEntryInput
from n4_support import N4Flow, codes, pfmea_entries


@pytest.mark.parametrize("s, o, d, level", [(10, 1, 1, "HIGH"), (7, 4, 2, "HIGH"), (5, 5, 6, "HIGH"),
                                            (5, 3, 2, "MEDIUM"), (4, 9, 9, "LOW"), (6, 2, 4, "LOW")])
def test_custom_attention_heuristic(s, o, d, level):
    assert attention(s, o, d)[0] == level


def test_the_heuristic_never_claims_aiag_vda():
    assert "NOT AIAG-VDA Action Priority" in HEURISTIC_LABEL and "illustrative" in HEURISTIC_LABEL
    e = rate_entry(PfmeaEntryInput(**pfmea_entries()[0]), "te", None)
    assert e.action_priority is None and e.rated_by == "te" and "NOT AIAG-VDA" in e.attention_basis


def _ap_table(tmp_path):
    table = {str(s): {str(o): {str(d): ("H" if s >= 9 else "M" if s * o >= 20 else "L") for d in range(1, 11)}
                      for o in range(1, 11)} for s in range(1, 11)}
    path = tmp_path / "ap_table_TEST_FIXTURE.json"                    # a test fixture, not the licensed table
    path.write_text(json.dumps(table))
    return str(path)


def test_ap_lookup_is_used_only_with_a_configured_licensed_table(tmp_path, make_n1):
    from app.n4.config import N4Settings
    from n3_support import n3_settings
    assert ApTable.load(_ap_table(tmp_path), None) is None                         # no licence → heuristic only
    app = make_n1(n3_settings=n3_settings(tmp_path / "j"),
                  n4_settings=N4Settings(ap_table_path=_ap_table(tmp_path), ap_license_ref="LIC-AP-TEST-001"))
    flow = N4Flow(app)
    i, p, a = flow.verified()
    pf = flow.c.get(f"/v1/n4/plans/{p['plan_id']}", "aud").json()["pfmea"]
    assert pf["prioritization_method"] == "AIAG_VDA_AP_LICENSED" and pf["ap_license_ref"] == "LIC-AP-TEST-001"
    assert [e["action_priority"] for e in pf["entries"]] == ["H", "H", "H", "M"]
    flow.n4_review(a)
    rel = flow.n4_release(p["plan_id"], targets=("N8",)).json()
    assert rel["status"] == "RELEASED"
    payload = flow.c.get("/v1/artifacts/VER-PSU-120W-N8", "aud").json()["canonical_artifact"]["payload"]
    assert payload["prioritization_method"] == "AIAG_VDA_AP_LICENSED" and payload["ap_license_ref"]


@pytest.fixture
def planned(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"])
    flow4.full_evidence(p["plan_id"])
    return flow4, p["plan_id"]


def test_entries_cannot_cite_a_suggestion_that_does_not_exist(planned):
    flow, pid = planned
    e = {**pfmea_entries()[3], "adopted_from_suggestion": "N4SG-nope"}
    r = flow.pfmea(pid, [e])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "SUGGESTION_NOT_FOUND"


@pytest.mark.parametrize("change, code", [
    (lambda es: es[0].update(actions=[{"action_id": "A-1", "description": "add a second hipot station",
                                       "owner": "te"}]), "PFMEA_SAFETY_ACTION_OPEN"),
    (lambda es: es[1].update(detection_controls=[{"description": "visual check"}]), "PFMEA_SAFETY_DETECTION_UNMAPPED"),
    (lambda es: es[3].update(actions=[{"action_id": "A-2", "description": "replace stencil", "owner": "te",
                                       "status": "VERIFIED"}]), "ACTION_VERIFICATION_UNSUPPORTED"),
])
def test_risk_based_pfmea_blocks(planned, change, code):
    flow, pid = planned
    es = pfmea_entries()
    change(es)
    assert flow.pfmea(pid, es).status_code == 201
    a = flow.n4_assess(pid)
    assert a["decision"] == "BLOCK" and code in codes(a, "BLOCK")


def test_a_verified_action_needs_a_capa_verified_effective(planned):
    flow, pid = planned
    flow.c.post("/v1/n4/capas", "te", json={"capa_id": "CAPA-7", "plan_id": pid, "title": "stencil wear",
                                            "pfmea_entry_ids": ["FM-04"], "root_cause": "stencil life exceeded",
                                            "corrective_action": "stencil cycle counter", "owner": "te"})
    es = pfmea_entries()
    es[3]["actions"] = [{"action_id": "A-2", "description": "stencil counter", "owner": "te", "capa_id": "CAPA-7",
                         "status": "VERIFIED"}]
    flow.pfmea(pid, es)
    assert "ACTION_VERIFICATION_UNSUPPORTED" in codes(flow.n4_assess(pid))          # CAPA still OPEN
    ev = flow.c.get(f"/v1/n4/plans/{pid}", "aud").json()["evidence"][0]["evidence_id"]
    flow.c.post("/v1/n4/capas/CAPA-7/verify", "qr", json={"evidence_id": ev, "effective": True, "notes": "AOI clean"})
    a = flow.n4_assess(pid)
    assert a["decision"] == "PASS", [(f["code"], f["message"]) for f in a["findings"] if f["outcome"] != "INFO"]


def test_missing_pfmea_and_unaddressed_hazards_are_rework(planned):
    flow, pid = planned
    assert "PFMEA_MISSING" in codes(flow.n4_assess(pid), "REWORK")
    flow.pfmea(pid, [e for e in pfmea_entries() if e["entry_id"] != "FM-02"])
    a = flow.n4_assess(pid)
    assert a["decision"] == "REWORK" and any(f["code"] == "PFMEA_HAZARD_UNADDRESSED" and "H-2" in f["message"]
                                             for f in a["findings"])


def test_high_attention_is_advisory_and_never_decides(planned):
    flow, pid = planned
    es = pfmea_entries()
    es[3].update(severity=6, occurrence=6, detection=7)                              # HIGH by the heuristic
    flow.pfmea(pid, es)
    a = flow.n4_assess(pid)
    hi = [f for f in a["findings"] if f["code"] == "PFMEA_HIGH_ATTENTION" and "FM-04" in f["message"]]
    assert a["decision"] == "PASS" and hi and hi[0]["outcome"] == "INFO"


def test_control_plan_references_plan_tests(planned):
    flow, pid = planned
    bad = [{"characteristic": "x", "test_id": "T-99", "measurement_method": "m", "sample_size": 1,
            "frequency": "lot", "reaction_plan": "hold the lot"}]
    assert flow.c.post(f"/v1/n4/plans/{pid}/control-plan", "te", json={"items": bad}).status_code == 422
