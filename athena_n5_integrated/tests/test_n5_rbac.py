"""Procurement RBAC (signed JWTs only, no demo API keys) and separation of duties between
procurement_analyst, supplier_quality and procurement_approver."""
from __future__ import annotations

import pytest

from n5_support import BOURNS, MURATA, offer


@pytest.fixture
def sourced(flow5):
    intake, rep = flow5.sourced()
    return flow5, intake, rep


@pytest.mark.parametrize("who, path", [
    ("sq", "/v1/n5/bom-intakes"), ("pap", "/v1/n5/bom-intakes"), ("te", "/v1/n5/bom-intakes"),
    ("pan", "/v1/n5/reviews"), ("sq", "/v1/n5/reviews"), ("pan", "/v1/n5/reports/{rid}/handoffs"),
    ("pan", "/v1/n5/suppliers/SUP-DIST-A/review"), ("pap", "/v1/n5/suppliers/SUP-DIST-A/review"),
    ("pap", "/v1/n5/quotes"), ("aud", "/v1/n5/reports"), ("sq", "/v1/n5/fx-assumptions"),
    ("fo", "/v1/n6/intake"), ("co", "/v1/n7/intake"), ("lo", "/v1/n6/intake"),
])
def test_role_matrix(sourced, who, path):
    flow, intake, rep = sourced
    bodies = {
        "/v1/n5/bom-intakes": {"prd_id": "PSU-120W", "project_id": "PRJ-PSU-120W", "build_quantity": 1,
                               "target_lead_time_days": 1, "release_source_id": "SRC-PRC"},
        "/v1/n5/reviews": {"report_id": rep["report_id"], "report_sha256": rep["report_sha256"],
                           "quote_hashes": rep["quote_hashes"], "decision": "APPROVED", "reason": "role check"},
        "/v1/n5/reports/{rid}/handoffs": {"targets": ["N6"]},
        "/v1/n5/suppliers/SUP-DIST-A/review": {"decision": "SUSPENDED", "attestation": "role check, nothing else here"},
        "/v1/n5/quotes": {"supplier_id": "SUP-DIST-A", "bom_intake_id": intake["intake_id"], "design_revision": "B",
                          "quote_ref": "R", "issued_at": "2026-11-20T00:00:00Z", "valid_until": "2030-01-01T00:00:00Z",
                          "source_id": "SRC-QUOTES", "offers": [offer(1, MURATA, "1")]},
        "/v1/n5/reports": {"bom_intake_id": intake["intake_id"]},
        "/v1/n5/fx-assumptions": {"fx_id": "FX-R", "from_currency": "USD", "to_currency": "INR", "rate": "1",
                                  "as_of": "2026-11-20", "source": "role check"},
        "/v1/n6/intake": {"artifact_id": "PRC-PSU-120W-N6", "revision": "A"},
        "/v1/n7/intake": {"artifact_id": "PRC-PSU-120W-N7", "revision": "A"},
    }
    r = flow.c.post(path.format(rid=rep["report_id"]), who, json=bodies[path])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ROLE_REQUIRED", r.text


def test_no_demo_keys_only_signed_jwts(n5):
    assert n5.c.get("/v1/n5/status").status_code == 401
    assert n5.c.get("/v1/n5/status", headers={"X-API-Key": "N0_DEMO_API_KEY"}).status_code == 401
    assert n5.c.get("/v1/n5/status", headers={"Authorization": "Bearer N0_APPROVER_KEY"}).status_code == 401


def test_the_report_author_cannot_approve_it(flow5):
    flow5.bom_released()
    flow5.standard_suppliers()
    intake = flow5.n5_intake()
    flow5.demo_quotes(intake)
    rep = flow5.n5_report(intake, who="dualp")
    r = flow5.n5_review(rep, who="dualp")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
    assert flow5.n5_review(rep, who="pap").status_code == 201


def test_a_quote_submitter_cannot_approve_or_release(flow5):
    flow5.bom_released()
    flow5.standard_suppliers()
    intake = flow5.n5_intake()
    m, b = flow5.L[MURATA], flow5.L[BOURNS]
    assert flow5.quote(intake, "SUP-DIST-A", [offer(m, MURATA, "6.50"), offer(b, BOURNS, "9")],
                       who="dualp").status_code == 201
    rep = flow5.n5_report(intake)
    assert flow5.n5_review(rep, who="dualp").json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
    assert flow5.n5_review(rep, who="pap").status_code == 201
    r = flow5.n5_handoff(rep, who="dualp")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
    assert flow5.n5_handoff(rep, who="pap2").status_code == 201


def test_the_qualifier_of_a_selected_supplier_cannot_approve(flow5):
    flow5.bom_released()
    flow5.supplier("SUP-DIST-A", reviewer="sqp")                         # qualified by a person who can also approve
    intake = flow5.n5_intake()
    m, b = flow5.L[MURATA], flow5.L[BOURNS]
    flow5.quote(intake, "SUP-DIST-A", [offer(m, MURATA, "6.50"), offer(b, BOURNS, "9")])
    rep = flow5.n5_report(intake)
    r = flow5.n5_review(rep, who="sqp")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"


def test_rejected_reviews_are_recorded_and_block_the_handoff(sourced):
    flow, intake, rep = sourced
    assert flow.n5_review(rep, decision="REJECTED", reason="prices need a second quote").status_code == 201
    r = flow.n5_handoff(rep)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "APPROVAL_REQUIRED"


def test_tenants_are_isolated(sourced):
    flow, intake, rep = sourced
    assert flow.c.get(f"/v1/n5/bom-intakes/{intake['intake_id']}", "gpan").status_code == 404
    assert flow.c.get(f"/v1/n5/reports/{rep['report_id']}", "gpan").status_code == 404
    r = flow.quote(intake, "SUP-DIST-A", [offer(1, MURATA, "1")], who="gpan")
    assert r.status_code == 404
