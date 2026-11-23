"""Vendor atlas integration: import, analytics, due diligence, connectors and vendor jobs
(reference workbook rules G4, ING-03/06/10, DF-026 and tests T10, T27, T32, T33, T35)."""
import base64
import hashlib
import io
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.atlas import jobs as jobs_mod
from app.atlas.signatures import sign
from app.main import create_app

from .conftest import make_settings

ATLAS = Path(__file__).parent.parent / "data" / "reference" / "ATHENA_Global_Specialized_Vendor_Atlas_2026.xlsx"
ROLE_USER = {"vendor_lead": "vendor_lead", "engineer": "engineer", "qa": "qa", "finance": "finance", "programme_manager": "pm"}


@pytest.fixture
def ac(tmp_path):
    """Client with the real atlas seeded (1,163 records) and the 47 reference connectors."""
    client = TestClient(create_app(make_settings(tmp_path, seed_atlas=True)))
    cache = {}

    def h(user):
        if user not in cache:
            tok = client.post("/auth/login", json={"username": user, "password": f"{user}-demo"}).json()["access_token"]
            cache[user] = {"Authorization": f"Bearer {tok}"}
        return cache[user]

    client.h = h
    client.tmp = tmp_path
    return client


def qualify_atlas(ac, atlas_id, overrides=None):
    criteria = ac.get("/atlas/due-diligence/criteria", headers=ac.h("pm")).json()["criteria"]
    for c in criteria:
        score, passed = (overrides or {}).get(c["key"], (4, True))
        r = ac.put(f"/atlas/vendors/{atlas_id}/due-diligence/{c['key']}", headers=ac.h(ROLE_USER[c["assessor_roles"][0]]),
                   json={"score": score, "passed": passed, "evidence": [f"evidence/{atlas_id}/{c['key']}.pdf"]})
        assert r.status_code == 200, r.text
    return ac.post(f"/atlas/vendors/{atlas_id}/due-diligence/decision", headers=ac.h("founder"),
                   json={"decision": "qualified", "notes": "pilot"})


def make_ready(ac, atlas_id, connector_id):
    """Atlas record → due diligence → engagement vendor → entitlement → active connector. Returns the webhook secret."""
    assert qualify_atlas(ac, atlas_id).status_code == 200
    vendor = ac.post(f"/atlas/vendors/{atlas_id}/promote", headers=ac.h("vendor_lead"), json={"connector_id": connector_id}).json()
    assert ac.post(f"/vendors/{vendor['id']}/qualify", headers=ac.h("vendor_lead")).status_code == 200
    ac.post(f"/vendor-connectors/{connector_id}/entitlement", headers=ac.h("vendor_lead"),
            json={"evidence": ["order-form-2026-11.pdf", "api-docs-v3 checked", "test call log #12"]})
    r = ac.post(f"/vendor-connectors/{connector_id}/activate", headers=ac.h("pm"))
    assert r.status_code == 200, r.text
    return r.json()["webhook_secret"]


def thread_inputs(ac, consent="VENDOR_ALLOWED", key="RFQ-V1"):
    rfq = ac.post("/thread/revisions", headers=ac.h("engineer"),
                  json={"kind": "RFQ", "key": key, "content": {"raw_text": "24 V 5 A controller", "consent_scope": consent}}).json()
    prd = ac.post("/thread/revisions", headers=ac.h("engineer"),
                  json={"kind": "PRD", "key": key, "content": {"requirements": ["24 V input"]}, "derived_from": [rfq["id"]]}).json()
    return rfq, prd


def run(ac, connector_id, prd_id, key=None):
    wp = {"input_revision_ids": [prd_id], "deliverable": "schematic draft + BOM", "budget": {"amount": "250.00", "currency": "USD"}}
    return ac.post(f"/vendor-connectors/{connector_id}/run", headers=ac.h("engineer"),
                   json={"work_package": wp, "idempotency_key": key or uuid.uuid4().hex})


def callback(ac, connector_id, secret, payload, nonce=None, ts=None, tamper=False):
    body = json.dumps(payload).encode()
    ts = ts or int(time.time())
    nonce = nonce or uuid.uuid4().hex
    sig = sign(secret, ts, nonce, body + (b" " if tamper else b""))
    return ac.post(f"/vendor-callbacks/pilot/{connector_id}", content=body,
                   headers={"X-Athena-Timestamp": str(ts), "X-Athena-Nonce": nonce, "X-Athena-Signature": sig,
                            "Content-Type": "application/json"})


def result_payload(job_id, content=b"(kicad_sch (version 20231120))", declared=None):
    return {"event_id": uuid.uuid4().hex, "job_id": job_id, "type": "job.result", "tool_version": "flux-2026.11",
            "result": {"summary": "schematic draft"},
            "files": [{"name": "draft.kicad_sch", "sha256": declared or hashlib.sha256(content).hexdigest(),
                       "content_b64": base64.b64encode(content).decode()}]}


# --- import and analytics ---------------------------------------------------------------

def test_import_reproduces_the_atlas_analytics_sheet(ac):
    f = ac.get("/atlas/facets", headers=ac.h("viewer")).json()
    assert (f["records"], f["provisional_parents"], f["pilot"]) == (1163, 1074, 47)
    assert f["price_evidence"]["Published numerical entry price"] == 10
    sheet = [r for r in load_workbook(ATLAS, read_only=True, data_only=True)["CATEGORY ANALYTICS"]
             .iter_rows(min_row=2, values_only=True) if r[0]]
    mine = {s["segment"]: s for s in f["segments"]}
    assert len(sheet) == len(mine) == 31
    for seg, records, directory, price, pilot, nodes in sheet:
        s = mine[seg]
        assert (s["records"], s["directory_sourced"], s["numeric_public_price"], s["pilot"], s["node_mappings"]) == \
               (records, directory, price, pilot, [nodes]), seg


def test_prices_are_never_fabricated(ac):
    numeric = ac.get("/atlas/vendors?price_evidence=Published numerical entry price&limit=50", headers=ac.h("viewer")).json()
    assert numeric["total"] == 10 and all(v["price"]["numeric_public_entry"] for v in numeric["items"])
    flux = ac.get("/atlas/vendors/AT-0282", headers=ac.h("viewer")).json()
    assert flux["price"]["numeric_public_entry"] is False
    assert flux["price"]["terms"] == flux["source"]["price_terms"]  # verbatim text, no derived number
    assert flux["connectors"] == ["CON-008", "CON-016"] and flux["qualified"] is False


def test_search_filters(ac):
    r = ac.get("/atlas/vendors?node=N5&selection=Pilot&limit=500", headers=ac.h("viewer")).json()
    assert r["total"] > 0 and all("N5" in v["nodes"] and v["selection"] == "Pilot" for v in r["items"])
    assert [v["name"] for v in ac.get("/atlas/vendors?q=luminovo", headers=ac.h("viewer")).json()["items"]] == \
           ["Luminovo", "Luminovo Network"]
    llm = ac.get("/atlas/vendors?node=N1&segment=LLMOps / models / agent infrastructure&limit=500", headers=ac.h("viewer")).json()
    assert llm["total"] == 78  # "N1-N8" range expanded


def _edited_atlas(edit) -> bytes:
    wb = load_workbook(ATLAS)
    edit(wb["VENDOR MASTER"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_reimport_is_idempotent_updates_changes_and_keeps_athena_state(ac):
    up = lambda data: ac.post("/atlas/imports?filename=atlas.xlsx", content=data, headers=ac.h("vendor_lead"))  # noqa: E731
    again = up(ATLAS.read_bytes())
    assert again.status_code == 201 and again.json()["duplicate"] is True
    ac.put("/atlas/vendors/AT-0002/outreach", headers=ac.h("vendor_lead"), json={"status": "Contacted", "note": "intro call"})

    def edit(ws):
        ws["J3"] = "Per site / per year"   # AT-0002 billing basis changes
        ws.delete_rows(4)                  # AT-0003 disappears from the newer file
    r = up(_edited_atlas(edit)).json()
    assert r["stats"] == {"rows": 1162, "inserted": 0, "updated": 1, "unchanged": 1161, "withdrawn": 1}
    v = ac.get("/atlas/vendors/AT-0002", headers=ac.h("viewer")).json()
    assert v["source"]["billing_basis"] == "Per site / per year" and v["outreach_status"] == "Contacted"
    assert ac.get("/atlas/vendors/AT-0003", headers=ac.h("viewer")).json()["in_latest_import"] is False
    assert ac.get("/atlas/facets", headers=ac.h("viewer")).json()["records"] == 1162


def test_bad_files_are_rejected_with_row_feedback(ac):
    up = lambda data: ac.post("/atlas/imports", content=data, headers=ac.h("vendor_lead"))  # noqa: E731
    assert up(b"ID,Vendor\nAT-1,x").json()["detail"].startswith("not an .xlsx")
    no_header = up(_edited_atlas(lambda ws: ws.__setitem__("E1", "Nodes"))).json()
    assert no_header["missing_columns"] == ["ATHENA N1-N8 mapping"]

    def corrupt(ws):
        ws["E2"] = "N9"
        ws["A3"] = "AT-0001"
    bad = up(_edited_atlas(corrupt))
    body = bad.json()
    assert bad.status_code == 422 and body["rejected_rows"] == 2
    assert [(e["row"], e["problems"]) for e in body["errors"]] == [
        (2, ["invalid node token 'N9'"]), (3, ["duplicate ID AT-0001"])]
    statuses = [i["status"] for i in ac.get("/atlas/imports", headers=ac.h("viewer")).json()]
    assert statuses[0] == "rejected" and ac.get("/atlas/facets", headers=ac.h("viewer")).json()["records"] == 1163
    assert up(b"x").status_code == 422 and \
        ac.post("/atlas/imports", content=ATLAS.read_bytes(), headers=ac.h("engineer")).status_code == 403


# --- connectors & crosswalk ---------------------------------------------------------------

def test_connector_registry_and_crosswalk(ac):
    conns = ac.get("/vendor-connectors", headers=ac.h("viewer")).json()
    assert len(conns) == 47 and all(c["readiness"]["state"] == "reference" for c in conns)
    basis = {}
    for c in conns:
        basis[c["atlas_match_basis"]] = basis.get(c["atlas_match_basis"], 0) + 1
        for atlas_id in c["atlas_ids"]:
            assert ac.get(f"/atlas/vendors/{atlas_id}", headers=ac.h("viewer")).status_code == 200
    assert basis == {"exact_name": 27, "curated": 14, "not_in_atlas": 6}
    hubspot = next(c for c in conns if c["connector_id"] == "CON-033")
    assert "not in the vendor atlas" in hubspot["readiness"]["blockers"][1]
    stack = ac.get("/atlas/stack?nodes=N5", headers=ac.h("viewer")).json()["N5"]
    assert [c["connector_id"] for c in stack["connectors"]] == ["CON-027", "CON-028", "CON-029", "CON-030", "CON-031", "CON-032"]
    assert stack["pilot_candidates"] and all(not c["ready"] for c in stack["connectors"])


def test_activation_requires_entitlement_and_due_diligence(ac):
    pm, vl = ac.h("pm"), ac.h("vendor_lead")
    assert ac.post("/vendor-connectors/CON-008/activate", headers=pm).status_code == 409  # no entitlement
    ac.post("/vendor-connectors/CON-008/entitlement", headers=vl, json={"evidence": ["plan confirmation"]})
    r = ac.post("/vendor-connectors/CON-008/activate", headers=pm)
    assert r.status_code == 409 and "no linked atlas record has passed due diligence" in r.json()["blockers"]
    assert ac.post("/vendor-connectors/CON-008/activate", headers=ac.h("engineer")).status_code == 403


# --- due diligence ---------------------------------------------------------------------------

def test_due_diligence_scoring_roles_and_knockouts(ac):
    url = "/atlas/vendors/AT-0582/due-diligence"
    ok = {"score": 5, "passed": True, "evidence": ["msa.pdf"]}
    assert ac.put(f"{url}/commercial_rights", headers=ac.h("engineer"), json=ok).status_code == 403  # owned by ATHENA/vendor lead
    assert ac.put(f"{url}/commercial_rights", headers=ac.h("vendor_lead"), json={**ok, "evidence": []}).status_code == 422
    r = qualify_atlas(ac, "AT-0582", {"input_confidentiality": (5, False)})  # knock-out fails despite high scores
    assert r.status_code == 409 and any("input_confidentiality: knock-out" in b for b in r.json()["blockers"])

    low = qualify_atlas(ac, "AT-0583", {k: (2, True) for k in ("price_predictability", "deliverable_acceptance",
                                                               "commercial_rights", "input_confidentiality")})
    assert low.status_code == 409 and any("weighted score" in b for b in low.json()["blockers"])

    good = qualify_atlas(ac, "AT-0564")
    assert good.status_code == 200 and good.json()["decision"] == "qualified" and good.json()["weighted_score"] == 4.0
    assert ac.get("/atlas/vendors/AT-0564", headers=ac.h("viewer")).json()["outreach_status"] == "Qualified"
    # a later failing finding reopens due diligence and revokes qualification
    ac.put(f"/atlas/vendors/AT-0564/due-diligence/commercial_rights", headers=ac.h("vendor_lead"),
           json={"score": 1, "passed": False, "evidence": ["licence forbids redistribution"]})
    v = ac.get("/atlas/vendors/AT-0564", headers=ac.h("viewer")).json()
    assert v["due_diligence"] == "open" and v["qualified"] is False


def test_promotion_requires_qualification_and_a_linked_connector(ac):
    vl = ac.h("vendor_lead")
    assert ac.post("/atlas/vendors/AT-0282/promote", headers=vl, json={"connector_id": "CON-008"}).status_code == 409
    qualify_atlas(ac, "AT-0282")
    assert ac.post("/atlas/vendors/AT-0282/promote", headers=vl, json={"connector_id": "CON-027"}).status_code == 422
    v = ac.post("/atlas/vendors/AT-0282/promote", headers=vl, json={"connector_id": "CON-008"}).json()
    assert v["license_verified"] and v["data_terms_accepted"] and v["mode"] == "file_handoff"
    assert ac.post("/atlas/vendors/AT-0282/promote", headers=vl, json={"connector_id": "CON-008"}).status_code == 409


# --- vendor jobs -------------------------------------------------------------------------------

def test_vendor_job_end_to_end_with_lineage_and_review(ac):
    secret = make_ready(ac, "AT-0282", "CON-008")
    rfq, prd = thread_inputs(ac)
    job = run(ac, "CON-008", prd["id"], key="flux-job-1").json()
    assert job["status"] == "AWAITING_VENDOR" and job["dispatch"]["channel"] == "file_handoff"
    manifest = json.loads(Path(job["dispatch"]["path"]).read_text())
    assert manifest["job"]["inputs"][0]["revision_id"] == prd["id"] and "secret" not in json.dumps(manifest)
    assert run(ac, "CON-008", prd["id"], key="flux-job-1").json()["id"] == job["id"]  # idempotent

    r = callback(ac, "CON-008", secret, result_payload(job["id"]))
    assert r.status_code == 200 and r.json()["status"] == "RESULT_RECEIVED"
    j = ac.get(f"/vendor-jobs/{job['id']}", headers=ac.h("viewer")).json()
    lineage = ac.get(f"/thread/revisions/{j['result_revision_id']}/lineage", headers=ac.h("viewer")).json()
    assert lineage["revision"]["kind"] == "SIM" and {"RFQ", "PRD"} <= set(lineage["kinds"])
    pid = ac.get("/programmes", headers=ac.h("pm")).json()[0]["id"]
    gate_t = lambda: ac.post(f"/programmes/{pid}/activities/T/check", json={}, headers=ac.h("qa")).json()["results"]  # noqa: E731
    assert next(x for x in gate_t() if x["name"] == "auto.vendor_results_decided")["passed"] is False

    assert ac.post(f"/vendor-jobs/{job['id']}/review", headers=ac.h("engineer"), json={"accept": True}).status_code == 403
    done = ac.post(f"/vendor-jobs/{job['id']}/review", headers=ac.h("qa"), json={"accept": True, "notes": "ERC clean"}).json()
    assert done["status"] == "ACCEPTED"
    assert next(x for x in gate_t() if x["name"] == "auto.vendor_results_decided")["passed"] is True


def test_t35_no_consent_no_dispatch_then_release(ac):
    make_ready(ac, "AT-0282", "CON-008")
    rfq, prd = thread_inputs(ac, consent="INTERNAL_ONLY", key="RFQ-PRIV")
    job = run(ac, "CON-008", prd["id"]).json()
    assert job["status"] == "BLOCKED_PERMISSION" and job["dispatch"] == {} and job["open_exceptions"] == ["consent_missing"]
    assert not list((ac.tmp / "outbox").rglob("*.json"))
    ac.post("/thread/revisions", headers=ac.h("engineer"),
            json={"kind": "RFQ", "key": "RFQ-PRIV", "content": {"raw_text": "24 V 5 A controller", "consent_scope": "VENDOR_ALLOWED"}})
    released = ac.post(f"/vendor-jobs/{job['id']}/retry", headers=ac.h("engineer")).json()
    assert released["status"] == "AWAITING_VENDOR" and released["open_exceptions"] == []


def test_t10_licence_hold_after_due_diligence_lapses(ac):
    make_ready(ac, "AT-0282", "CON-008")
    _, prd = thread_inputs(ac)
    ac.put("/atlas/vendors/AT-0282/due-diligence/commercial_rights", headers=ac.h("vendor_lead"),
           json={"score": 1, "passed": False, "evidence": ["export prohibits customer redistribution"]})
    job = run(ac, "CON-008", prd["id"]).json()
    assert job["status"] == "HELD_LICENSE" and job["open_exceptions"] == ["license_hold"]
    pid = ac.get("/programmes", headers=ac.h("pm")).json()[0]["id"]
    l_check = ac.post(f"/programmes/{pid}/activities/L/check", json={}, headers=ac.h("pm")).json()["results"][0]
    assert l_check["detail"]["due_diligence_lapsed"] == ["Flux"]


def test_t27_rate_limit_retries_without_double_charge(ac):
    secret = make_ready(ac, "AT-0564", "CON-027")
    calls = []

    class FlakyNexar:
        def submit(self, conn, vendor, job):
            calls.append(job.idempotency_key)
            if len(calls) == 1:
                raise jobs_mod.RetryableVendorError("429 Too Many Requests", retry_after=0)
            return {"vendor_request_id": "nx-1", "idempotency_key": job.idempotency_key}

    jobs_mod.ADAPTERS["CON-027"] = FlakyNexar()
    try:
        _, prd = thread_inputs(ac)
        job = run(ac, "CON-027", prd["id"], key="bom-lookup-1").json()
        assert job["status"] == "QUEUED" and job["attempts"] == 1
        with ac.app.state.sessionmaker() as db:
            assert jobs_mod.dispatch_due(db, ac.app.state.settings) == 1
        job = ac.get(f"/vendor-jobs/{job['id']}", headers=ac.h("viewer")).json()
        assert job["status"] == "AWAITING_VENDOR" and calls == ["bom-lookup-1", "bom-lookup-1"]
        charge = {"type": "job.charge", "job_id": job["id"], "charges": [{"charge_ref": "inv-77", "amount": "4.20", "currency": "USD"}]}
        callback(ac, "CON-027", secret, {**charge, "event_id": "e1"})
        callback(ac, "CON-027", secret, {**charge, "event_id": "e2"})  # vendor resends the same charge
        job = ac.get(f"/vendor-jobs/{job['id']}", headers=ac.h("viewer")).json()
        assert [c["charge_ref"] for c in job["charges"]] == ["inv-77"]
    finally:
        jobs_mod.ADAPTERS.pop("CON-027", None)


def test_t32_charge_without_artifact_is_an_open_exception(ac):
    secret = make_ready(ac, "AT-0282", "CON-008")
    _, prd = thread_inputs(ac)
    job = run(ac, "CON-008", prd["id"]).json()
    callback(ac, "CON-008", secret, {"event_id": "c1", "type": "job.charge", "job_id": job["id"],
                                     "charges": [{"charge_ref": "ch-1", "amount": "300", "currency": "USD"}]})
    j = ac.get(f"/vendor-jobs/{job['id']}", headers=ac.h("viewer")).json()
    assert set(j["open_exceptions"]) == {"charge_without_artifact", "over_budget"}
    callback(ac, "CON-008", secret, result_payload(job["id"]))
    j = ac.get(f"/vendor-jobs/{job['id']}", headers=ac.h("viewer")).json()
    assert j["open_exceptions"] == ["over_budget"]  # artifact arrived; the budget overrun stays open


def test_t33_checksum_mismatch_is_quarantined(ac):
    secret = make_ready(ac, "AT-0282", "CON-008")
    _, prd = thread_inputs(ac)
    job = run(ac, "CON-008", prd["id"]).json()
    r = callback(ac, "CON-008", secret, result_payload(job["id"], declared="0" * 64))
    assert r.json()["status"] == "QUARANTINED"
    j = ac.get(f"/vendor-jobs/{job['id']}", headers=ac.h("viewer")).json()
    assert j["result_revision_id"] is None and j["open_exceptions"] == ["checksum_mismatch"]
    assert "quarantine" in j["result_files"][0]["stored_at"]
    assert ac.post(f"/vendor-jobs/{job['id']}/review", headers=ac.h("qa"), json={"accept": True}).status_code == 409


def test_callback_signature_replay_and_window(ac):
    secret = make_ready(ac, "AT-0282", "CON-008")
    _, prd = thread_inputs(ac)
    job = run(ac, "CON-008", prd["id"]).json()
    status = {"event_id": "s1", "type": "job.status", "job_id": job["id"], "status": "in_progress"}
    assert callback(ac, "CON-008", secret, status, tamper=True).status_code == 401
    assert callback(ac, "CON-008", "wrong-secret", status).status_code == 401
    assert callback(ac, "CON-008", secret, status, ts=int(time.time()) - 3600).status_code == 401
    assert callback(ac, "CON-016", secret, status).status_code == 401  # inactive connector reveals nothing
    assert callback(ac, "CON-008", secret, status, nonce="nonce-0001").status_code == 200
    assert callback(ac, "CON-008", secret, {**status, "event_id": "s2"}, nonce="nonce-0001").status_code == 409
    assert callback(ac, "CON-008", secret, status).json()["event"] == "duplicate_ignored"
    security = ac.get("/ai/evals/security", headers=ac.h("viewer")).json()
    assert security["checks"]["all_routes_authenticated"]["passed"] is True


def test_pipeline_rfqs_default_to_internal_only(client, auth):
    from .conftest import pipeline_input
    run_ = client.post("/workflows", json={"definition": "rfq_to_quote", "input": pipeline_input("RFQ-C")},
                       headers=auth("engineer")).json()
    rid = next(s for s in run_["steps"] if s["name"] == "ingest_rfq")["output"]["rfq_revision_id"]
    assert client.get(f"/thread/revisions/{rid}", headers=auth("viewer")).json()["content"]["consent_scope"] == "INTERNAL_ONLY"
    bad = client.post("/workflows", json={"definition": "rfq_to_quote", "input": {**pipeline_input("RFQ-D"), "consent_scope": "ANY"}},
                      headers=auth("engineer")).json()
    assert bad["status"] == "FAILED"
