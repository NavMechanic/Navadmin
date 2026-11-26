"""Role-scoped JWT RBAC and separation of duties: design_engineer, vendor_submitter and design_authority.
There are no demo API keys; every identity is a signed JWT (``conftest.token``)."""
from __future__ import annotations

import pytest

from conftest import token
from n3_support import VENDOR, PACKAGE_FILES, PACKAGE_JOBS, design_files, sha


@pytest.fixture
def ready(flow):
    b = flow.ready()
    wps = flow.plan(b["baseline_id"], ["schematic_pcb"])
    return flow, b, wps


@pytest.mark.parametrize("who, method, path, body", [
    ("ven", "post", "/v1/n3/baselines", {"prd_id": "PSU-120W", "project_id": "P1", "design_source_id": "SRC-DESIGN"}),
    ("ven", "post", "/v1/n3/reviews", {"assessment_id": "N3A-x", "decision": "APPROVED", "reason": "vendor says ok"}),
    ("de", "post", "/v1/n3/reviews", {"assessment_id": "N3A-x", "decision": "APPROVED", "reason": "engineer ok"}),
    ("de", "post", "/v1/n3/vendors", {"vendor_id": "VEN-X", "tenant_id": "acme", "name": "x", "capabilities": ["DFM"],
                                     "ip_terms": "ASSIGNED_TO_CUSTOMER", "nda_on_file": True,
                                     "max_budget": {"amount": "1", "currency": "USD"}}),
    ("da", "post", "/v1/n3/baselines", {"prd_id": "PSU-120W", "project_id": "P1", "design_source_id": "SRC-DESIGN"}),
    ("aud", "post", "/v1/n3/baselines", {"prd_id": "PSU-120W", "project_id": "P1", "design_source_id": "SRC-DESIGN"}),
    ("pa", "get", "/v1/n3/status", None),
    ("ven", "get", "/v1/n3/status", None),
])
def test_role_matrix(n3, who, method, path, body):
    r = getattr(n3, method)(path, who, **({"json": body} if body is not None else {}))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ROLE_REQUIRED", r.text


def test_unauthenticated_and_forged_tokens_are_refused(n3):
    assert n3.c.get("/v1/n3/status").status_code == 401
    assert n3.c.get("/v1/n3/status", headers={"Authorization": "Bearer tok-demo"}).status_code == 401
    assert n3.c.get("/v1/n3/status", headers={"X-API-Key": "N0_DEMO_API_KEY"}).status_code == 401


def test_vendor_delivers_only_to_packages_routed_to_its_vendor_id(ready):
    flow, b, wps = ready
    wp = wps["PCB_LAYOUT"]["work_package_id"]
    f = {n: design_files()[n] for n in PACKAGE_FILES["PCB_LAYOUT"]}
    r = flow.deliver(wp, f, who="ven")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "VENDOR_NOT_ROUTED"
    assert flow.onboard().status_code == 201 and flow.route_vendor(wp).status_code == 201
    other = flow.deliver(wp, f, who="ven2")                                    # vendor_id VEN-OTHER
    assert other.status_code == 403 and other.json()["detail"]["code"] == "VENDOR_NOT_ROUTED"
    no_claim = flow.c.c.post(f"/v1/n3/work-packages/{wp}/deliveries",
                             headers={"Authorization": f"Bearer {token('ven', vendor_id=None)}"},
                             data={"manifest": "{}"}, files=[("files", ("x.csv", b"a,b\n1,2\n", "text/csv"))])
    assert no_claim.status_code == 403
    ok = flow.deliver(wp, f, who="ven")
    assert ok.status_code == 201, ok.text
    body = ok.json()
    assert body["vendor_id"] == VENDOR and all(x["origin"] == "VENDOR" for x in body["files"])
    assert body["n0_vendor_result_id"]                                         # N0 VendorResult.v1 recorded


def test_vendor_sees_only_its_own_work(ready):
    flow, b, wps = ready
    wp = wps["PCB_LAYOUT"]["work_package_id"]
    assert flow.c.get(f"/v1/n3/work-packages/{wp}", "ven").status_code == 403
    flow.onboard()
    flow.route_vendor(wp)
    view = flow.c.get(f"/v1/n3/work-packages/{wp}", "ven").json()
    assert set(view) == {"work_package", "deliveries"}                         # no assessments / other parties
    assert flow.c.get(f"/v1/n3/baselines/{b['baseline_id']}", "ven").status_code == 403
    assert flow.c.get(f"/v1/artifacts/{b['prd_artifact'].split('@')[0]}", "ven").status_code == 403


def _deliver_and_run(flow, wp, who):
    kind = wp["kind"]
    f = {n: design_files()[n] for n in PACKAGE_FILES[kind]}
    flow.route_tool(wp["work_package_id"], "kicad-cli")
    assert flow.deliver(wp["work_package_id"], f, who=who).status_code == 201
    for tool, op, names in PACKAGE_JOBS[kind]:
        assert flow.job(wp["work_package_id"], tool, op, [sha(f[n]) for n in names]).status_code == 201


def test_a_submitter_cannot_approve_their_own_delivery(ready):
    flow, b, wps = ready
    _deliver_and_run(flow, wps["ELECTRICAL_SCHEMATIC"], who="dual")           # engineer + authority roles
    a = flow.assess(wps["ELECTRICAL_SCHEMATIC"]["work_package_id"])
    r = flow.review(a["assessment_id"], who="dual")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
    assert flow.review(a["assessment_id"], who="da").status_code == 201


def test_an_assessor_cannot_approve_their_own_assessment(ready):
    flow, b, wps = ready
    _deliver_and_run(flow, wps["ELECTRICAL_SCHEMATIC"], who="de")
    a = flow.assess(wps["ELECTRICAL_SCHEMATIC"]["work_package_id"], who="dual")
    r = flow.review(a["assessment_id"], who="dual")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"


def test_a_submitter_cannot_release_their_own_delivery(ready):
    flow, b, wps = ready
    _deliver_and_run(flow, wps["ELECTRICAL_SCHEMATIC"], who="dual")
    for kind in ("PCB_LAYOUT", "DFM"):
        _deliver_and_run(flow, wps[kind], who="de")
    for kind, wp in wps.items():
        a = flow.assess(wp["work_package_id"])
        assert a["decision"] == "PASS" and flow.review(a["assessment_id"], who="da2").status_code == 201
    r = flow.release(b["baseline_id"], "schematic_pcb", who="dual")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
    assert flow.release(b["baseline_id"], "schematic_pcb", who="da").status_code == 201


def test_tenants_are_isolated(ready):
    flow, b, wps = ready
    assert flow.c.get(f"/v1/n3/baselines/{b['baseline_id']}", "gde").status_code == 404
    wp = wps["PCB_LAYOUT"]["work_package_id"]
    assert flow.c.post(f"/v1/n3/work-packages/{wp}/assess", "gde").status_code == 404
    r = flow.c.post("/v1/n3/vendors", "da", json={
        "vendor_id": "VEN-X", "tenant_id": "globex", "name": "x", "capabilities": ["DFM"],
        "ip_terms": "ASSIGNED_TO_CUSTOMER", "nda_on_file": True, "max_budget": {"amount": "1", "currency": "USD"}})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "CROSS_TENANT_REFERENCE"
