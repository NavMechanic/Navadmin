"""Invariant 2 (N2 baseline lock) and revision control: a new PRD revision invalidates dependent work;
replaced deliveries make their derived evidence stale; mixed design revisions never release."""
from __future__ import annotations

from n3_support import PACKAGE_FILES, PACKAGE_JOBS, codes, design_files, gen, sha


def _revise_prd(flow):
    rec = flow.prd()
    assert rec["revision"] == "B"
    return rec


def test_new_prd_revision_invalidates_baseline_packages_and_releases(flow):
    b = flow.ready()
    flow.scope(b["baseline_id"], "circuit_simulation")
    rel = flow.release(b["baseline_id"], "circuit_simulation")
    assert rel.status_code == 201
    _revise_prd(flow)
    st = lambda kind, i: flow.c.get(f"/v1/n3/subjects/{kind}/{i}/status", "aud").json()  # noqa: E731
    assert st("baseline", b["baseline_id"])["status"] == "INVALIDATED"
    assert "revised A → B" in st("baseline", b["baseline_id"])["history"][-1]["reason"]
    for wid in rel.json()["work_package_ids"]:
        assert st("work_package", wid)["status"] == "INVALIDATED"
    assert st("release", rel.json()["release_id"])["status"] == "INVALIDATED"
    assert flow.c.get(f"/v1/n3/releases/{rel.json()['release_id']}", "de").json()["status"] == "INVALIDATED"
    events = flow.c.get("/v1/audit/events?limit=1000", "aud").json()
    assert any(e["event_type"] == "N3_BASELINE_INVALIDATED" for e in events)


def test_every_n3_operation_on_an_invalidated_baseline_is_refused(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], ["circuit_simulation"])
    wp = wps["CIRCUIT_SIMULATION"]["work_package_id"]
    flow.route_tool(wp, "ngspice")
    net = design_files()["psu_buck.cir"]
    assert flow.deliver(wp, {"psu_buck.cir": net}).status_code == 201
    _revise_prd(flow)
    calls = [flow.c.post(f"/v1/n3/baselines/{b['baseline_id']}/plan", "de",
                         json={"budget_per_package": {"amount": "1", "currency": "USD"}}),
             flow.route_tool(wp, "ngspice"), flow.deliver(wp, {"psu_buck.cir": net}),
             flow.job(wp, "ngspice", "spice_corners", [sha(net)]),
             flow.c.post(f"/v1/n3/work-packages/{wp}/assess", "de"),
             flow.release(b["baseline_id"], "circuit_simulation")]
    assert [(r.status_code, r.json()["detail"]["code"]) for r in calls] == [(409, "BASELINE_INVALIDATED")] * 6


def test_invalidation_is_also_enforced_lazily(flow):
    b = flow.ready()
    flow.c.app.state.n2.release_listeners.clear()                         # e.g. N2 running in another process
    _revise_prd(flow)
    assert flow.c.get(f"/v1/n3/subjects/baseline/{b['baseline_id']}/status", "de").json()["status"] == "ACTIVE"
    r = flow.c.post(f"/v1/n3/baselines/{b['baseline_id']}/plan", "de",
                    json={"budget_per_package": {"amount": "1", "currency": "USD"}})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BASELINE_INVALIDATED"
    assert flow.c.get(f"/v1/n3/subjects/baseline/{b['baseline_id']}/status", "de").json()["status"] == "INVALIDATED"


def test_only_the_newest_prd_revision_can_be_locked(flow):
    old = flow.ready()
    _revise_prd(flow)
    assert flow.baseline(prd_revision="A", expect=409)["detail"]["code"] == "PRD_SUPERSEDED"
    new = flow.baseline()
    assert new["baseline_id"] != old["baseline_id"] and new["prd_artifact"] == "PRD-PSU-120W@B"
    assert flow.scope(new["baseline_id"], "circuit_simulation")["CIRCUIT_SIMULATION"]["decision"] == "PASS"


def test_replacing_a_delivery_makes_derived_evidence_stale(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["PCB_LAYOUT"]
    first = flow.work_package(wp)
    assert first["decision"] == "PASS" and first["design_revision"] == "B"
    rev_c = {"psu.kicad_sch": gen.schematic(rev="C").encode(), "psu.kicad_pcb": gen.board(rev="C").encode(),
             "psu.kicad_pro": gen.project().encode(), "psu_bom.csv": gen.bom_csv().encode()}
    assert flow.deliver(wp["work_package_id"], rev_c, revision="C").status_code == 201
    stale = flow.assess(wp["work_package_id"])                              # the rev-B DRC report no longer counts
    assert stale["decision"] == "REWORK" and "VERIFICATION_EVIDENCE_MISSING" in codes(stale)
    assert {f["path"] for f in stale["files"]} == set(rev_c)
    j = flow.job(wp["work_package_id"], "kicad-cli", "pcb_drc",
                 [sha(rev_c[n]) for n in ("psu.kicad_pcb", "psu.kicad_sch", "psu.kicad_pro")])
    assert j.json()["status"] == "SUCCEEDED"
    fresh = flow.assess(wp["work_package_id"])
    assert fresh["decision"] == "PASS" and fresh["design_revision"] == "C"
    stale_review = flow.review(first["assessment_id"])
    assert stale_review.status_code == 409 and stale_review.json()["detail"]["code"] == "STALE_ASSESSMENT"


def test_merging_a_new_board_revision_into_an_old_set_blocks(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["schematic_pcb"])["PCB_LAYOUT"]
    flow.work_package(wp)
    pcb_c = gen.board(rev="C").encode()
    assert flow.deliver(wp["work_package_id"], {"psu.kicad_pcb": pcb_c}, revision="C", replaces=False).status_code == 201
    a = flow.assess(wp["work_package_id"])
    assert a["decision"] == "BLOCK" and {"REVISION_MISMATCH", "BOM_REVISION_MISMATCH"} <= codes(a, "BLOCK")


def test_packages_at_different_design_revisions_never_release_together(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], ["schematic_pcb"])
    rev_c = design_files(**{"psu.kicad_sch": gen.schematic(rev="C"), "psu.kicad_pcb": gen.board(rev="C")})
    for kind, files, rev in (("ELECTRICAL_SCHEMATIC", design_files(), "B"), ("PCB_LAYOUT", rev_c, "C"),
                             ("DFM", rev_c, "C")):
        wp = wps[kind]
        flow.route_tool(wp["work_package_id"], "kicad-cli")
        pkg = {n: files[n] for n in PACKAGE_FILES[kind]}
        assert flow.deliver(wp["work_package_id"], pkg, revision=rev).status_code == 201
        for tool, op, names in PACKAGE_JOBS[kind]:
            assert flow.job(wp["work_package_id"], tool, op, [sha(pkg[n]) for n in names]).status_code == 201
        a = flow.assess(wp["work_package_id"])
        assert a["decision"] == "PASS" and a["design_revision"] == rev
        assert flow.review(a["assessment_id"]).status_code == 201
    r = flow.release(b["baseline_id"], "schematic_pcb")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DESIGN_REVISION_INCONSISTENT"
