"""JWT / RBAC: no demo API keys, signed tokens only, roles separated, tenant taken from the token."""
from __future__ import annotations

import time

import jwt
import pytest

from conftest import ISSUER, SECRET, award, token


def _h(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def test_only_valid_signed_tokens_are_accepted(n1):
    path = "/v1/n1/reports"
    assert n1.c.get(path).status_code == 401
    assert n1.c.get(path, headers=_h("N0_DEMO_API_KEY")).status_code == 401          # no demo keys any more
    assert n1.c.get(path, headers=_h(token("ana", exp=int(time.time()) - 600))).status_code == 401
    assert n1.c.get(path, headers=_h(token("ana", iss="https://other.example"))).status_code == 401
    forged = jwt.encode({"sub": "ana", "tenant_id": "acme", "roles": ["approver"], "iss": ISSUER,
                         "exp": int(time.time()) + 60}, "not-the-secret-" + "z" * 30, algorithm="HS256")
    assert n1.c.get(path, headers=_h(forged)).status_code == 401
    no_tenant = jwt.encode({"sub": "ana", "roles": ["analyst"], "iss": ISSUER, "exp": int(time.time()) + 60},
                           SECRET, algorithm="HS256")
    assert n1.c.get(path, headers=_h(no_tenant)).status_code == 401
    assert n1.c.get(path, headers=_h(token("ana", roles=["god_mode"]))).status_code == 401
    assert n1.c.get(path, headers=_h(token("ana"))).status_code == 200


@pytest.mark.parametrize("who, allowed", [("ana", {"batch", "report", "optimize"}),
                                          ("apr", {"attest", "handoff", "slm_evaluate", "optimize"}),
                                          ("svc", {"batch"}),
                                          ("aud", set())])
def test_roles_are_separated(n1_src, who, allowed):
    n1_src.submit_ok(award())
    rep = n1_src.report()
    calls = {
        "batch": lambda: n1_src.submit(award("TED-AUTH-2"), who=who, batch_id="B-AUTH"),
        "report": lambda: n1_src.post("/v1/n1/reports", who, json={"tenant_id": "acme",
                                                                   "serviceable_categories": ["power_electronics"]}),
        "optimize": lambda: n1_src.post("/v1/n1/optimize", who, json={"tenant_id": "acme", "currency": "USD", "actions": [
            {"action_id": "A1", "action_type": "DESK_RESEARCH", "currency": "USD", "execution_cost": "1",
             "expected_loss_avoided": "2"}]}),
        "attest": lambda: n1_src.attest("TED-2026-000202", who=who),
        "handoff": lambda: n1_src.post(f"/v1/n1/reports/{rep['report_id']}/handoff", who,
                                       json={"targets": ["N6"], "derived_source_id": "SRC-TED",
                                             "project_id": "PRJ-MKT-2027"}),
        "slm_evaluate": lambda: n1_src.post("/v1/n1/slm/evaluate", who),
    }
    for name, call in calls.items():
        status = call().status_code
        if name in allowed:
            assert status != 403, f"{who} should be allowed to {name}"
        else:
            assert status == 403, f"{who} must not {name} (got {status})"
    assert n1_src.get(f"/v1/n1/reports/{rep['report_id']}", who).status_code == 200   # everyone in the tenant reads


def test_attester_cannot_be_the_submitter(n1_src):
    n1_src.submit_ok(award(), who="pat")                                             # pat holds analyst + approver
    r = n1_src.attest("TED-2026-000202", who="pat")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FOUR_EYES"


def test_refusals_are_audited(n1_src):
    n1_src.attest("TED-NOPE", who="ana")
    events = n1_src.get("/v1/audit/events", "aud").json()
    assert any(e["event_type"] == "N1_ATTESTATION_REFUSED" and e["details"]["code"] == "ROLE_REQUIRED" for e in events)
