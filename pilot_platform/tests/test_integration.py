"""Activity U: one RFQ spans the nodes with lineage — end to end over HTTP."""
from .conftest import pipeline_input


def start(client, auth, payload, user="engineer"):
    r = client.post("/workflows", json={"definition": "rfq_to_quote", "input": payload}, headers=auth(user))
    assert r.status_code == 201, r.text
    return r.json()


def step(run, name):
    return next(s for s in run["steps"] if s["name"] == name)


def decide(client, auth, run_id, step_name, user, approve=True):
    return client.post(f"/workflows/{run_id}/steps/{step_name}/decision", json={"approve": approve, "notes": "t"},
                       headers=auth(user))


def test_rfq_to_issued_quote_with_full_lineage(client, auth, pid):
    run = start(client, auth, pipeline_input())
    assert run["status"] == "WAITING_APPROVAL" and step(run, "review_extraction")["status"] == "WAITING_APPROVAL"
    assert step(run, "extract_rfq")["output"]["needs_review"] is False

    assert decide(client, auth, run["id"], "review_extraction", "engineer").status_code == 403  # initiator
    run = decide(client, auth, run["id"], "review_extraction", "engineer2").json()
    assert run["status"] == "WAITING_APPROVAL" and step(run, "approve_quote")["status"] == "WAITING_APPROVAL"
    costing = step(run, "costing")["output"]
    assert step(run, "sourcing")["output"]["supplier"] == "DistA"
    assert step(run, "sourcing")["output"]["unit_bom_cost"] == "3.2000"

    assert decide(client, auth, run["id"], "approve_quote", "engineer2").status_code == 403  # wrong role
    run = decide(client, auth, run["id"], "approve_quote", "founder").json()
    assert run["status"] == "SUCCEEDED"

    quote_id = step(run, "issue_quote")["output"]["quote_revision_id"]
    quote = client.get(f"/thread/revisions/{quote_id}", headers=auth("viewer")).json()
    assert quote["content"]["status"] == "issued" and quote["content"]["approved_by"] == "founder"
    assert quote["content"]["costing"]["outputs"]["price_ex_gst"] == costing["price_ex_gst"]
    lineage = client.get(f"/thread/revisions/{quote_id}/lineage", headers=auth("viewer")).json()
    assert {"RFQ", "PRD", "SIM", "TEST", "BOM", "QUOTE"} <= set(lineage["kinds"])

    pm = auth("pm")
    for code in ("E", "K", "M", "N", "O", "Q", "U"):
        result = client.post(f"/programmes/{pid}/activities/{code}/check", json={"evidence": {}}, headers=pm).json()
        assert result["passed"] is True, (code, result)


def test_rejected_quote_voids_the_draft(client, auth):
    run = start(client, auth, pipeline_input("RFQ-REJ"))
    decide(client, auth, run["id"], "review_extraction", "engineer2")
    run = decide(client, auth, run["id"], "approve_quote", "founder", approve=False).json()
    assert run["status"] == "FAILED" and step(run, "costing")["compensated"] is True
    quotes = client.get("/thread/artifacts?kind=QUOTE", headers=auth("viewer")).json()
    latest = client.get(f"/thread/revisions/{quotes[0]['latest_revision_id']}", headers=auth("viewer")).json()
    assert latest["content"]["status"] == "void" and latest["rev"] == 2


def test_business_rule_failure_stops_without_retry(client, auth):
    run = start(client, auth, pipeline_input("RFQ-ONEBID", bids=1))
    run = decide(client, auth, run["id"], "review_extraction", "engineer2").json()
    s = step(run, "sourcing")
    assert run["status"] == "FAILED" and s["status"] == "FAILED" and s["attempts"] == 1
    assert "comparable bid" in s["error"]


def test_single_source_with_justification_proceeds(client, auth):
    run = start(client, auth, pipeline_input("RFQ-SOLE", bids=1, justification="customer-mandated sole source"))
    run = decide(client, auth, run["id"], "review_extraction", "engineer2").json()
    assert step(run, "sourcing")["status"] == "SUCCEEDED"


def test_idempotent_workflow_start(client, auth):
    body = {"definition": "rfq_to_quote", "input": pipeline_input("RFQ-IDEM"), "idempotency_key": "rfq-idem-1"}
    a = client.post("/workflows", json=body, headers=auth("engineer")).json()
    b = client.post("/workflows", json=body, headers=auth("engineer")).json()
    assert a["id"] == b["id"]
    assert client.post("/workflows", json={"definition": "selftest.linear"}, headers=auth("engineer")).status_code == 422
