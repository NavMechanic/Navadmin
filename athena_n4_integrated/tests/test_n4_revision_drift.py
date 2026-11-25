"""Upstream revision changes invalidate N4 work: a new N2 PRD revision or a newer N3 design revision
invalidates the intake, its plans and releases (via listeners, and lazily), and N8 then refuses the
stale package."""
from __future__ import annotations

from n4_support import sha
from n3_support import PACKAGE_FILES, PACKAGE_JOBS, design_files, gen


def _revise_prd(flow):
    body = flow.prd_body(scopes=["schematic_pcb", "circuit_simulation"])
    r = flow.c.post("/v1/n2/prds", "pm", json=body)
    assert r.status_code == 200 and r.json()["revision"] == "B", r.text
    assert flow.c.post("/v1/n2/prds/PSU-120W/revisions/B/release", "pa",
                       json={"reason": "revised ripple requirement"}).status_code == 200


def _redesign_schematic_pcb(flow, baseline_id):
    """N3 delivers, verifies and releases schematic_pcb at design revision C under the same PRD."""
    files = design_files(**{"psu.kicad_sch": gen.schematic(rev="C"), "psu.kicad_pcb": gen.board(rev="C")})
    wps = flow.plan(baseline_id, ["schematic_pcb"])
    for kind, wp in wps.items():
        pkg = {n: files[n] for n in PACKAGE_FILES[kind]}
        assert flow.deliver(wp["work_package_id"], pkg, revision="C").status_code == 201
        for tool, op, names in PACKAGE_JOBS[kind]:
            assert flow.job(wp["work_package_id"], tool, op, [sha(pkg[n]) for n in names]).status_code == 201
        a = flow.assess(wp["work_package_id"])
        assert a["decision"] == "PASS" and flow.review(a["assessment_id"]).status_code == 201
    r = flow.release(baseline_id, "schematic_pcb", targets=("N4",))
    assert r.status_code == 201 and r.json()["design_revision"] == "C", r.text


def test_new_prd_revision_invalidates_intake_plan_and_refuses_all_work(flow4):
    i, p, a = flow4.verified()
    _revise_prd(flow4)
    st = flow4.c.get(f"/v1/n4/subjects/intake/{i['intake_id']}/status", "aud").json()
    assert st["status"] == "INVALIDATED" and "revised A → B" in st["history"][-1]["reason"]
    assert flow4.c.get(f"/v1/n4/subjects/plan/{p['plan_id']}/status", "aud").json()["status"] == "INVALIDATED"
    pid = p["plan_id"]
    calls = [flow4.c.post(f"/v1/n4/plans/{pid}/assess", "te"), flow4.bench(pid, "T-01", serial="PSU-0009"),
             flow4.pfmea(pid), flow4.n4_review(a), flow4.n4_release(pid),
             flow4.c.post(f"/v1/n4/intakes/{i['intake_id']}/plans", "te", json={"title": "again", "test_cases": []})]
    assert [r.status_code for r in calls[:5]] == [409] * 5
    assert {r.json()["detail"]["code"] for r in calls[:5]} == {"INTAKE_INVALIDATED"}


def test_newer_n3_design_revision_invalidates_the_intake(flow4):
    b = flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"])
    _redesign_schematic_pcb(flow4, b["baseline_id"])
    st = flow4.c.get(f"/v1/n4/subjects/intake/{i['intake_id']}/status", "aud").json()
    assert st["status"] == "INVALIDATED" and "DSN-PSU-120W-schematic_pcb@C" in st["history"][-1]["reason"]
    assert flow4.c.post(f"/v1/n4/plans/{p['plan_id']}/assess", "te").json()["detail"]["code"] == "INTAKE_INVALIDATED"
    new = flow4.intake()
    assert new["intake_id"] != i["intake_id"] and new["design_revision"] == new["pcb_revision"] == "C"
    assert {d["artifact"] for d in new["design_refs"]} == {"DSN-PSU-120W-schematic_pcb@C",
                                                           "DSN-PSU-120W-circuit_simulation@B"}


def test_invalidation_is_also_enforced_lazily(flow4):
    flow4.designed()
    flow4.registries()
    i = flow4.intake()
    p = flow4.n4_plan(i["intake_id"])
    flow4.c.app.state.n2.release_listeners.clear()                 # e.g. N2 running in another process
    _revise_prd(flow4)
    assert flow4.c.get(f"/v1/n4/subjects/intake/{i['intake_id']}/status", "aud").json()["status"] == "LOCKED"
    r = flow4.c.post(f"/v1/n4/plans/{p['plan_id']}/assess", "te")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INTAKE_INVALIDATED"
    assert flow4.c.get(f"/v1/n4/subjects/intake/{i['intake_id']}/status", "aud").json()["status"] == "INVALIDATED"


def test_n8_refuses_a_package_whose_n4_release_was_invalidated(flow4):
    i, p, a = flow4.verified()
    flow4.n4_review(a)
    rel = flow4.n4_release(p["plan_id"], targets=("N8",)).json()
    assert rel["status"] == "RELEASED"
    _revise_prd(flow4)
    assert flow4.c.get(f"/v1/n4/releases/{rel['release_id']}", "aud").json()["status"] == "INVALIDATED"
    r = flow4.c.post("/v1/n8/intake", "lo", json={"artifact_id": "VER-PSU-120W-N8", "revision": "A"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "N4_RELEASE_NOT_ACTIVE"
