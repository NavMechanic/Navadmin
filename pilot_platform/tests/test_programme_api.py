"""AND-join starts, gate reviews, segregation of duties and automated gate checks over HTTP."""

GOOD_A = {"buyer_interviews": 10, "bounded_use_cases": 1}


def url(pid, code, action=""):
    return f"/programmes/{pid}/activities/{code}" + (f"/{action}" if action else "")


def test_and_join_blocks_start_until_every_predecessor_passes(client, auth, pid):
    r = client.post(url(pid, "B", "start"), headers=auth("founder"))
    assert r.status_code == 409
    assert r.json()["error"] == "gate_blocked"
    assert r.json()["blocking_predecessors"] == {"A": "NOT_STARTED"}
    r = client.post(url(pid, "F", "start"), headers=auth("engineer"))
    assert set(r.json()["blocking_predecessors"]) == {"B", "E"}


def test_owner_role_is_enforced(client, auth, pid):
    assert client.post(url(pid, "A", "start"), headers=auth("engineer")).status_code == 403
    assert client.post(url(pid, "A", "start"), headers=auth("viewer")).status_code == 403
    assert client.post(url(pid, "A", "start"), headers=auth("founder")).status_code == 200


def test_full_gate_cycle_with_rework(client, auth, pid):
    founder, reviewer = auth("founder"), auth("reviewer")
    client.post(url(pid, "A", "start"), headers=founder)
    r = client.post(url(pid, "A", "submit"), json={"evidence": {"buyer_interviews": 4, "bounded_use_cases": 1}}, headers=founder)
    assert r.status_code == 200 and r.json()["checks"]["passed"] is False

    assert client.post(url(pid, "A", "decide"), json={"decision": "pass"}, headers=founder).status_code == 403
    r = client.post(url(pid, "A", "decide"), json={"decision": "pass"}, headers=reviewer)
    assert r.status_code == 409 and r.json()["failing_checks"] == ["evidence.buyer_interviews min 10"]

    assert client.post(url(pid, "A", "decide"), json={"decision": "fail", "notes": "need more interviews"},
                       headers=reviewer).json()["decision"] == "failed"
    assert client.post(url(pid, "A", "rework"), headers=founder).json()["status"] == "IN_PROGRESS"
    client.post(url(pid, "A", "submit"), json={"evidence": GOOD_A, "actual_duration_days": 8}, headers=founder)
    r = client.post(url(pid, "A", "decide"), json={"decision": "pass"}, headers=reviewer)
    assert r.status_code == 200 and r.json()["decision"] == "passed"

    assert client.post(url(pid, "B", "start"), headers=founder).status_code == 200
    assert client.get(f"/programmes/{pid}", headers=founder).json()["status_counts"] == {
        "GATE_PASSED": 1, "IN_PROGRESS": 1, "NOT_STARTED": 24}
    reviews = client.get(url(pid, "A", "reviews"), headers=founder).json()
    assert [r["decision"] for r in reviews] == ["failed", "passed"]

    sched = client.get(f"/programmes/{pid}/schedule?use_actuals=true", headers=founder).json()
    assert abs(sched["expected_duration_days"] - (124.6667 + 8 - 31 / 6)) < 1e-3


def test_submitter_cannot_decide_their_own_gate(client, auth, pid):
    pm = auth("pm")  # programme_manager may both own and review, but never the same gate
    client.post(url(pid, "A", "start"), headers=pm)
    client.post(url(pid, "A", "submit"), json={"evidence": GOOD_A}, headers=pm)
    r = client.post(url(pid, "A", "decide"), json={"decision": "pass"}, headers=pm)
    assert r.status_code == 403 and "segregation" in r.json()["detail"]


def test_dry_run_check_does_not_change_state(client, auth, pid):
    r = client.post(url(pid, "A", "check"), json={"evidence": GOOD_A}, headers=auth("founder"))
    assert r.json()["passed"] is True
    assert client.get(url(pid, "A"), headers=auth("founder")).json()["status"] == "NOT_STARTED"


def test_platform_gates_are_checked_automatically(client, auth, pid):
    pm = auth("pm")

    def check(code, evidence=None):
        return client.post(url(pid, code, "check"), json={"evidence": evidence or {}}, headers=pm).json()

    assert check("D", {"role_test_report": "reports/rbac.xml"})["passed"] is True   # every route authenticated
    assert check("G")["passed"] is True   # isolation probe + budget cap
    assert check("H")["passed"] is True   # gold-set extraction accuracy
    assert check("I")["passed"] is True   # engine self-test
    assert check("F", {"schema_version": "rfq/1.0"})["passed"] is True
    assert check("E")["passed"] is False  # no artifacts yet: lineage gate fails closed

    rfq = client.post("/thread/revisions", json={"kind": "RFQ", "key": "R1", "content": {"raw_text": "x"}},
                      headers=auth("engineer")).json()
    client.post("/thread/revisions", json={"kind": "PRD", "key": "R1", "content": {"requirements": []},
                                           "derived_from": [rfq["id"]]}, headers=auth("engineer"))
    assert check("E")["passed"] is True
    for code in ("K", "M", "N", "O", "P", "Q", "R", "U"):  # need real node output first
        assert check(code)["passed"] is False, code


def test_gate_register_and_activity_view(client, auth, pid):
    rows = client.get(f"/programmes/{pid}/gate-register", headers=auth("viewer")).json()
    assert len(rows) == 26
    f = next(r for r in rows if r["code"] == "F")
    assert f["join"] == "AND(B, E)" and f["gate_criteria"] == "Defined fields, conflicts and provenance"
    assert client.get(url(pid, "C", "trace") + "?direction=up", headers=auth("viewer")).json()["activities"] == ["A", "B"]


def test_custom_plan_validation(client, auth):
    bad = {"name": "loop", "activities": [
        {"code": "A", "layer": "x", "name": "a", "predecessors": ["B"], "optimistic": 1, "most_likely": 2, "pessimistic": 3,
         "owner": "o", "owner_role": "engineer"},
        {"code": "B", "layer": "x", "name": "b", "predecessors": ["A"], "optimistic": 1, "most_likely": 2, "pessimistic": 3,
         "owner": "o", "owner_role": "engineer"}]}
    r = client.post("/programmes", json={"plan": bad}, headers=auth("pm"))
    assert r.status_code == 422 and "cycle" in r.json()["detail"]
    assert client.post("/programmes", json={}, headers=auth("engineer")).status_code == 403
