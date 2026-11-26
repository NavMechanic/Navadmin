"""Role-scoped JWT RBAC (no demo API keys) and separation of duties: the approver of a verification is a
separate, qualified test_authority / quality_lead, never an evidence submitter, the plan author or the
assessor; importers never adjudicate their own AOI calls; SOP authors and CAPA owners never sign off
their own work."""
from __future__ import annotations

from datetime import timedelta

import pytest

from n4_support import NOW, plan_cases


@pytest.fixture
def planned(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"])
    return flow4, i, p["plan_id"]


@pytest.mark.parametrize("who, method, path", [
    ("li", "post", "/v1/n4/plans/{pid}/assess"),
    ("li", "post", "/v1/n4/intakes"),
    ("te", "post", "/v1/n4/reviews"),
    ("te", "post", "/v1/n4/plans/{pid}/releases"),
    ("te", "post", "/v1/n4/adjudications"),
    ("te", "post", "/v1/n4/methods/SOP-ATE-001/revisions/C/approve"),
    ("qr", "post", "/v1/n4/reviews"),
    ("qr", "post", "/v1/n4/plans/{pid}/releases"),
    ("ta", "post", "/v1/n4/labs"),
    ("lo", "post", "/v1/n4/plans/{pid}/assess"),
    ("aud", "post", "/v1/n4/plans/{pid}/assess"),
    ("de", "post", "/v1/n4/intakes"),
])
def test_role_matrix(planned, who, method, path):
    flow, i, pid = planned
    bodies = {"/v1/n4/reviews": {"assessment_id": "N4A-x", "evidence_sha256": "a" * 64, "design_revision": "B",
                                 "decision": "APPROVED", "reason": "role check"},
              "/v1/n4/intakes": {"prd_id": "PSU-120W", "project_id": "PRJ-X", "release_source_id": "SRC-VER"},
              "/v1/n4/plans/{pid}/releases": {"targets": ["N8"]},
              "/v1/n4/adjudications": {"evidence_id": "x", "decisions": [], "unit_verdict": "ACCEPT",
                                       "notes": "role check"},
              "/v1/n4/labs": {"lab_id": "LAB-X", "name": "x", "accreditation_body": "x", "accreditation_number": "x",
                              "scopes": ["IEC 62368-1"], "valid_until": "2030-01-01"}}
    r = getattr(flow.c, method)(path.format(pid=pid), who, **({"json": bodies[path]} if path in bodies else {}))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ROLE_REQUIRED", r.text


def test_no_demo_keys_only_signed_jwts(n4):
    assert n4.c.get("/v1/n4/status").status_code == 401
    assert n4.c.get("/v1/n4/status", headers={"X-API-Key": "N0_DEMO_API_KEY"}).status_code == 401
    assert n4.c.get("/v1/n4/status", headers={"Authorization": "Bearer N0_APPROVER_KEY"}).status_code == 401


def test_an_evidence_submitter_cannot_approve(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"])
    flow4.pfmea(p["plan_id"])
    flow4.full_evidence(p["plan_id"])
    flow4.bench(p["plan_id"], "T-01", serial="PSU-0002", who="dual4")               # dual4 imports evidence …
    a = flow4.n4_assess(p["plan_id"])
    r = flow4.n4_review(a, who="dual4")                                              # … and tries to approve it
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"


def test_the_plan_author_and_the_assessor_cannot_approve(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"], who="dual4")
    flow4.pfmea(p["plan_id"])
    flow4.full_evidence(p["plan_id"])
    a = flow4.n4_assess(p["plan_id"])
    assert flow4.n4_review(a, who="dual4").json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
    a2 = flow4.n4_assess(p["plan_id"], who="te2")
    assert flow4.n4_review(a2, who="dual4").status_code == 403


def test_approver_must_hold_a_current_qualification_for_every_method(flow4):
    i, p, a = flow4.verified()
    r = flow4.n4_review(a, who="taq")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_QUALIFIED"
    flow4.c.post("/v1/n4/qualifications", "ql", json={"principal_id": "taq", "methods": ["BENCH_ATE", "SIM"],
                                                       "valid_until": (NOW + timedelta(days=30)).date().isoformat(),
                                                       "basis": "bench only"})
    assert "not qualified for" in flow4.n4_review(a, who="taq").json()["detail"]["message"]
    flow4.c.post("/v1/n4/qualifications", "ql", json={
        "principal_id": "taq", "methods": ["SIM", "BENCH_ATE", "ACCREDITED_LAB", "INSPECTION"],
        "valid_until": (NOW - timedelta(days=1)).date().isoformat(), "basis": "lapsed"})
    assert "expired" in flow4.n4_review(a, who="taq").json()["detail"]["message"]
    assert flow4.c.post("/v1/n4/qualifications", "ql", json={
        "principal_id": "ql", "methods": ["SIM"], "valid_until": "2030-01-01", "basis": "self"}).status_code == 403


def test_a_quality_lead_may_approve_when_qualified(flow4):
    i, p, a = flow4.verified()
    flow4.c.post("/v1/n4/qualifications", "ql", json={
        "principal_id": "ta2", "methods": ["SIM", "BENCH_ATE", "ACCREDITED_LAB", "INSPECTION"],
        "valid_until": (NOW + timedelta(days=30)).date().isoformat(), "basis": "renewed"})
    r = flow4.n4_review(a, who="ta2")
    assert r.status_code == 201 and r.json()["reviewer_role"] == "test_authority"


def test_a_submitter_cannot_release(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"])
    flow4.pfmea(p["plan_id"])
    flow4.full_evidence(p["plan_id"])
    flow4.bench(p["plan_id"], "T-01", serial="PSU-0002", who="dual4")
    a = flow4.n4_assess(p["plan_id"])
    assert flow4.n4_review(a, who="ta").status_code == 201
    r = flow4.n4_release(p["plan_id"], who="dual4")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
    assert flow4.n4_release(p["plan_id"], who="ta").status_code == 201


def test_capa_owner_and_sop_author_cannot_sign_off_their_own_work(planned):
    flow, i, pid = planned
    flow.method("SOP-X-1", "A", "BENCH_ATE", approve=False)
    assert flow.c.post("/v1/n4/methods/SOP-X-1/revisions/A/approve", "te").status_code == 403
    c = flow.c.post("/v1/n4/capas", "qr", json={"capa_id": "CAPA-Q", "plan_id": pid, "title": "wrong Y-cap",
                                                "owner": "qr", "pfmea_entry_ids": ["FM-02"],
                                                "root_cause": "feeder mix-up", "corrective_action": "feeder barcode"})
    assert c.status_code == 201, c.text
    ev = flow.bench(pid, "T-01").json()["evidence_id"]
    r = flow.c.post("/v1/n4/capas/CAPA-Q/verify", "qr", json={"evidence_id": ev, "effective": True, "notes": "self"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"


def test_tenants_are_isolated(planned):
    flow, i, pid = planned
    assert flow.c.get(f"/v1/n4/plans/{pid}", "gte").status_code == 404
    assert flow.c.post(f"/v1/n4/plans/{pid}/assess", "gte").status_code == 404
    r = flow.c.post("/v1/n4/instruments", "te", json={"instrument_id": "X-1", "tenant_id": "globex", "kind": "DMM",
                                                      "manufacturer": "m", "model": "m", "serial": "s"})
    assert r.status_code == 403 and len(plan_cases()) == 14
