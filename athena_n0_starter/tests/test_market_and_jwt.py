"""N0 support for market-intelligence artifacts (N1 inputs and N1 → N6/N7 outputs) and JWT auth."""
from __future__ import annotations

import copy
import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.rules import canonical_json, sha256_hex
from conftest import TOKENS, codes

NOW = "2026-11-20T09:00:00Z"


def _artifact(aid: str, atype: str, payload: dict, source_id: str, purpose: str, **kw) -> dict:
    return {"tenant_id": "acme", "project_id": "PRJ-MKT", "artifact_id": aid, "artifact_type": atype,
            "source_id": source_id, "processing_purpose": purpose, "release_stage": "CONCEPT", "revision": "A",
            "synthetic": False, "source_sha256": sha256_hex(canonical_json(payload)), "payload": payload, **kw}


def _batch(**obs_overrides) -> dict:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    obs = {"evidence_id": "EV-TED-1", "observation_type": "CONTRACT_AWARD", "source_connector": "TED_EU",
           "source_url": "https://ted.europa.eu/en/notice/-/detail/000001-2026", "buyer_name": "Example Water Board",
           "buyer_country": "DE", "amount": {"amount": "250000.00", "currency": "EUR"},
           "amount_kind": "AWARD_VALUE", "published_at": NOW, **obs_overrides}
    return {"batch_id": "B-1", "connector": "TED_EU", "retrieved_at": now, "observations": [obs]}


def test_market_batch_passes_and_routes_only_to_n1(n0):
    n0.source("SRC-TED", permitted_use=("MARKET_INTELLIGENCE",), license="CC-BY-4.0")
    res = n0.evaluate(_artifact("MB-1", "MARKET_BATCH", _batch(), "SRC-TED", "MARKET_INTELLIGENCE"))
    assert res["decision"]["decision"] == "PASS" and res["decision"]["allowed_destinations"] == ["N1"]


def test_one_synthetic_observation_taints_a_real_batch(n0):
    n0.source("SRC-TED", permitted_use=("MARKET_INTELLIGENCE",))
    res = n0.evaluate(_artifact("MB-2", "MARKET_BATCH", _batch(synthetic=True), "SRC-TED", "MARKET_INTELLIGENCE"))
    assert res["decision"]["decision"] == "BLOCK" and "SYNTHETIC_AS_REAL" in codes(res)


def _sam_payload(**over) -> dict:
    item = {"evidence_id": "EV-1", "account_id": "ACC-1", "spend_type": "VERIFIED_BUDGET", "synthetic": False,
            "attestation_id": "ATT-1", "original": {"amount": "100000.00", "currency": "EUR"},
            "fx_rate": "1.08", "fx_as_of": "2026-11-20", "amount_base": "108000.00",
            "source_url": "https://ted.europa.eu/x"}
    p = {"report_id": "RPT-1", "report_sha256": "a" * 64, "as_of": "2026-11-20", "base_currency": "USD",
         "verified_real_sam": "108000.00", "indicative_spend": "0", "hypothetical_addressable_spend": "0",
         "verified_items": [item], "caveats": []}
    p.update(over)
    return p


def test_sam_evidence_is_recomputed_by_n0(n0):
    n0.source("SRC-N1", permitted_use=("FINANCE",))
    ok = n0.evaluate(_artifact("SAM-1", "SAM_EVIDENCE", _sam_payload(), "SRC-N1", "FINANCE"))
    assert ok["decision"]["decision"] == "PASS" and ok["decision"]["allowed_destinations"] == ["N7"]
    bad_total = n0.evaluate(_artifact("SAM-2", "SAM_EVIDENCE", _sam_payload(verified_real_sam="999999.00"),
                                      "SRC-N1", "FINANCE"))
    assert bad_total["decision"]["decision"] == "REWORK" and "SAM_TOTAL_MISMATCH" in codes(bad_total)
    p = _sam_payload()
    p["verified_items"][0]["attestation_id"] = None
    unattested = n0.evaluate(_artifact("SAM-3", "SAM_EVIDENCE", p, "SRC-N1", "FINANCE"))
    assert unattested["decision"]["decision"] == "BLOCK" and "UNVERIFIED_SPEND_AS_VERIFIED" in codes(unattested)


def test_jwt_authentication_and_role_implications(tmp_path):
    secret = "test-secret-" + "x" * 32
    settings = Settings(database_url=f"sqlite:///{tmp_path}/j.db", storage_root=str(tmp_path / "o"),
                        tokens={}, jwt_secret=secret, jwt_issuer="https://idp.example", extra_roles=["approver"],
                        role_implications={"approver": ["release_manager", "gateway_reviewer"]})
    c = TestClient(create_app(settings))

    def tok(**claims):
        base = {"sub": "u1", "tenant_id": "acme", "roles": ["auditor"], "iss": "https://idp.example",
                "exp": int(time.time()) + 300}
        return {"Authorization": "Bearer " + pyjwt.encode({**base, **claims}, secret, algorithm="HS256")}

    assert c.get("/v1/audit/verify", headers=tok()).status_code == 200
    assert c.get("/v1/audit/verify", headers=tok(exp=int(time.time()) - 3600)).status_code == 401
    assert c.get("/v1/audit/verify", headers=tok(iss="https://evil.example")).status_code == 401
    assert c.get("/v1/audit/verify", headers=tok(roles=["superuser"])).status_code == 401
    forged = pyjwt.encode({"sub": "u1", "tenant_id": "acme", "roles": ["auditor"], "iss": "https://idp.example",
                           "exp": int(time.time()) + 300}, "wrong-secret-" + "y" * 32, algorithm="HS256")
    assert c.get("/v1/audit/verify", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    unsigned = pyjwt.encode({"sub": "u1", "tenant_id": "acme", "roles": ["auditor"], "exp": int(time.time()) + 300},
                            None, algorithm="none")
    assert c.get("/v1/audit/verify", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401
    principal = c.app.state.tokens.authenticate(tok(roles=["approver"])["Authorization"])
    assert {"approver", "release_manager", "gateway_reviewer"} <= set(principal.roles)
    assert principal.assigned_roles == ["approver"] and principal.auth_method == "jwt"


def test_none_algorithm_is_refused_at_configuration():
    from app.security import Authenticator
    with pytest.raises(ValueError):
        Authenticator({}, jwt_key="k" * 32, jwt_algorithms=["none"])
