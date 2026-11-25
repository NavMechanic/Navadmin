"""N2 (minimal product-intelligence slice): an N0-released RFQ becomes an N0-checked PRD that only a
separate product authority can release to N3."""
from __future__ import annotations

import json

import jsonschema
import pytest

from n3_support import CONSTRAINTS, ROOT, rfq


@pytest.fixture
def n2(flow):
    flow.sources()
    flow.rfq_to_n2()
    return flow


def test_prd_from_released_rfq_is_admitted_by_n0_and_routed_to_n3(n2):
    rec = n2.prd(release=False)
    assert rec["revision"] == "A" and rec["n0_decision"] == "PASS" and rec["source_rfq"] == "RFQ-PSU-120W@A"
    art = n2.c.get("/v1/artifacts/PRD-PSU-120W", "aud").json()
    assert art["canonical_artifact"]["artifact_type"] == "PRD"
    assert art["canonical_artifact"]["source_artifact_ids"] == ["RFQ-PSU-120W@A"]
    assert art["decision"]["allowed_destinations"] == ["N3"]
    payload = art["canonical_artifact"]["payload"]
    assert payload["design_constraints"]["input_voltage_max_v"] == 52.8 and payload["authored_by"] == "pm"


def test_prd_payload_matches_its_published_schema(n2):
    n2.prd(release=False)
    payload = n2.c.get("/v1/artifacts/PRD-PSU-120W", "aud").json()["canonical_artifact"]["payload"]
    schema = json.loads((ROOT / "schemas/payloads/PRDPayload.v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_rfq_must_have_been_released_to_n2(flow):
    flow.sources()
    art = rfq("RFQ-UNRELEASED")
    assert flow.c.post("/v1/gateway/evaluate", "ana", json={"artifact": art, "origin_node": "N0"}).status_code == 200
    r = flow.c.post("/v1/n2/prds", "pm", json=flow.prd_body(rfq_id="RFQ-UNRELEASED"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "RFQ_NOT_RELEASED_TO_N2"


def test_unknown_rfq_is_not_found(n2):
    r = n2.c.post("/v1/n2/prds", "pm", json=n2.prd_body(rfq_id="RFQ-NOPE"))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "RFQ_NOT_FOUND"


def test_superseded_rfq_revision_is_refused(n2):
    newer = rfq(revision="B")
    assert n2.c.post("/v1/gateway/evaluate", "ana", json={"artifact": newer, "origin_node": "N0"}).status_code == 200
    r = n2.c.post("/v1/n2/prds", "pm", json=n2.prd_body())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SUPERSEDED_REVISION"


def test_prd_revisions_increment_and_are_listed(n2):
    a, b = n2.prd(release=False), n2.prd(release=False)
    assert (a["revision"], b["revision"]) == ("A", "B")
    view = n2.c.get("/v1/n2/prds/PSU-120W", "de").json()
    assert [r["revision"] for r in view["revisions"]] == ["A", "B"] and view["releases"] == []


def test_release_records_an_approval_event_and_an_n0_manifest_to_n3(n2):
    rec = n2.prd()
    rel = rec["release"]
    assert rel["target_nodes"] == ["N3"] and rel["approved_by"] == "pa" and len(rel["approval_event_hash"]) == 64
    art = n2.c.get("/v1/artifacts/PRD-PSU-120W", "aud").json()
    released = [m for m in art["releases"] if m["status"] == "RELEASED"]
    assert [m["manifest_id"] for m in released] == [rel["manifest_id"]]
    assert released[0]["approval_id"] == rel["approval_id"] and released[0]["payload_sha256"] == rel["payload_sha256"]


def test_prd_author_cannot_release_their_own_prd(n2):
    r = n2.c.post("/v1/n2/prds", "pboth", json=n2.prd_body())
    assert r.status_code == 200
    rel = n2.c.post("/v1/n2/prds/PSU-120W/revisions/A/release", "pboth", json={"reason": "self approval"})
    assert rel.status_code == 403 and rel.json()["detail"]["code"] == "FOUR_EYES"


@pytest.mark.parametrize("who, path", [("de", "/v1/n2/prds"), ("pa", "/v1/n2/prds"),
                                       ("pm", "/v1/n2/prds/PSU-120W/revisions/A/release")])
def test_n2_roles_are_separated(n2, who, path):
    n2.prd(release=False)
    body = n2.prd_body() if path.endswith("prds") else {"reason": "not my role"}
    r = n2.c.post(path, who, json=body)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ROLE_REQUIRED"


def test_cross_tenant_prd_is_refused(n2):
    r = n2.c.post("/v1/n2/prds", "pm", json={**n2.prd_body(), "tenant_id": "globex"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "CROSS_TENANT_REFERENCE"


def test_prd_needs_at_least_one_design_scope_and_valid_constraints(n2):
    assert n2.c.post("/v1/n2/prds", "pm", json={**n2.prd_body(), "design_scopes": []}).status_code == 422
    bad = {**CONSTRAINTS, "derating_factor": 1.5}
    assert n2.c.post("/v1/n2/prds", "pm", json=n2.prd_body(constraints=bad)).status_code == 422


def test_releasing_an_unknown_revision_is_not_found(n2):
    n2.prd(release=False)
    r = n2.c.post("/v1/n2/prds/PSU-120W/revisions/C/release", "pa", json={"reason": "no such revision"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "PRD_NOT_FOUND"
