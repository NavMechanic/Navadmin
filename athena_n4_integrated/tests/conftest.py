"""Shared fixtures for the integrated N0 + N1 + N2 + N3 suite.

Every app gets its own temp SQLite database and object store. Authentication uses real HS256 JWTs
(no static API keys). There is one identity per role across two tenants, ``acme`` and ``globex``.
All market data here is fictional test data. Records marked ``synthetic`` stand for demo data;
unmarked records stand for real evidence inside the test.
"""
from __future__ import annotations

import copy
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.n1.config import N1Settings

ROOT = Path(__file__).resolve().parents[1]
SECRET = "athena-n1-test-secret-" + "k" * 40
ISSUER = "https://idp.example.test"
NOW = datetime.now(timezone.utc).replace(microsecond=0)

USERS = {
    "ana": ("acme", ["analyst"]), "ari": ("acme", ["analyst"]), "apr": ("acme", ["approver"]),
    "pat": ("acme", ["analyst", "approver"]), "svc": ("acme", ["service"]), "sam": ("acme", ["source_admin"]),
    "rita": ("acme", ["rights_approver"]), "aud": ("acme", ["auditor"]),
    "gina": ("globex", ["analyst"]), "gapr": ("globex", ["approver"]), "gadmin": ("globex", ["source_admin"]),
    "grights": ("globex", ["rights_approver"]),
    # N2 / N3 identities
    "pm": ("acme", ["product_manager"]), "pa": ("acme", ["product_authority"]),
    "pboth": ("acme", ["product_manager", "product_authority"]),
    "de": ("acme", ["design_engineer"]), "de2": ("acme", ["design_engineer"]),
    "da": ("acme", ["design_authority"]), "da2": ("acme", ["design_authority"]),
    "dual": ("acme", ["design_engineer", "design_authority"]),
    "ven": ("acme", ["vendor_submitter"]), "ven2": ("acme", ["vendor_submitter"]),
    "gde": ("globex", ["design_engineer"]),
    # N4 / N8 identities
    "te": ("acme", ["test_engineer"]), "te2": ("acme", ["test_engineer"]), "li": ("acme", ["lab_importer"]),
    "li2": ("acme", ["lab_importer"]), "qr": ("acme", ["quality_reviewer"]), "qr2": ("acme", ["quality_reviewer"]),
    "ta": ("acme", ["test_authority"]), "ta2": ("acme", ["test_authority"]), "ql": ("acme", ["quality_lead"]),
    "lo": ("acme", ["lifecycle_operator"]), "dual4": ("acme", ["test_engineer", "test_authority"]),
    "taq": ("acme", ["test_authority"]), "gte": ("globex", ["test_engineer"]),
}
VENDOR_CLAIMS = {"ven": "VEN-PCBWORKS", "ven2": "VEN-OTHER"}
MARKET_USES = ("MARKET_INTELLIGENCE", "RFQ_ANALYSIS", "COMMERCIAL", "FINANCE")


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def days(n: float) -> datetime:
    return NOW + timedelta(days=n)


def token(who: str, **claims: Any) -> str:
    tenant, roles = USERS[who]
    body = {"sub": who, "tenant_id": tenant, "roles": roles, "iss": ISSUER, "exp": int(time.time()) + 900, **claims}
    if who in VENDOR_CLAIMS:
        body.setdefault("vendor_id", VENDOR_CLAIMS[who])
    return jwt.encode(body, SECRET, algorithm="HS256")


# ------------------------------------------------------------------------------------------------
# fictional market evidence
# ------------------------------------------------------------------------------------------------
def tender(eid: str = "TED-2026-000101", **kw: Any) -> dict:
    base = {"evidence_id": eid, "observation_type": "TENDER_NOTICE", "source_connector": "TED_EU",
            "source_url": f"https://ted.europa.eu/en/notice/-/detail/{eid.removeprefix('TED-')}",
            "source_record_id": eid.removeprefix("TED-"), "buyer_name": "Stadtwerke Beispielstadt GmbH",
            "buyer_identifiers": {"vat": "DE999999999"}, "buyer_country": "DE", "buyer_type": "PUBLIC",
            "title": "Supply of DIN-rail power supply units", "classification_codes": {"cpv": ["31710000"]},
            "amount": {"amount": "1250000.00", "currency": "EUR"}, "amount_kind": "ESTIMATED_VALUE",
            "published_at": iso(days(-10)), "deadline_at": iso(days(25)), "quantity": 800,
            "requirements": [
                {"req_id": "T-1", "parameter": "output_voltage_v", "value": 24, "unit": "V", "value_origin": "CUSTOMER"},
                {"req_id": "T-2", "parameter": "output_current_a", "value": 10, "unit": "A", "value_origin": "CUSTOMER"},
                {"req_id": "T-3", "parameter": "operating_temp_min_c", "value": -25, "unit": "degC",
                 "value_origin": "CUSTOMER"},
                {"req_id": "T-4", "parameter": "operating_temp_max_c", "value": 70, "unit": "degC",
                 "value_origin": "CUSTOMER"}],
            "compliance_standards": ["IEC 62368-1"]}
    base.update(kw)
    return base


def award(eid: str = "TED-2026-000202", **kw: Any) -> dict:
    base = {"evidence_id": eid, "observation_type": "CONTRACT_AWARD", "source_connector": "TED_EU",
            "source_url": f"https://ted.europa.eu/en/notice/-/detail/{eid.removeprefix('TED-')}",
            "source_record_id": eid.removeprefix("TED-"), "buyer_name": "Stadtwerke Beispielstadt GmbH",
            "buyer_identifiers": {"vat": "DE999999999"}, "buyer_country": "DE", "buyer_type": "PUBLIC",
            "title": "Award: rectifier and battery charger maintenance", "classification_codes": {"cpv": ["31711000"]},
            "service_category": "power_electronics", "amount": {"amount": "400000.00", "currency": "EUR"},
            "amount_kind": "AWARD_VALUE", "published_at": iso(days(-60))}
    base.update(kw)
    return base


def sam_award(eid: str = "SAMGOV-ab12cd34", **kw: Any) -> dict:
    base = {"evidence_id": eid, "observation_type": "CONTRACT_AWARD", "source_connector": "SAM_GOV",
            "source_url": f"https://sam.gov/opp/{eid.removeprefix('SAMGOV-')}/view", "source_record_id": eid,
            "buyer_name": "Department of Example Affairs", "buyer_identifiers": {"sam_org_code": "099"},
            "buyer_country": "US", "buyer_type": "PUBLIC", "title": "Power supply modules for test racks",
            "classification_codes": {"naics": ["335999"]}, "amount": {"amount": "300000.00", "currency": "USD"},
            "amount_kind": "AWARD_VALUE", "published_at": iso(days(-30))}
    base.update(kw)
    return base


def batch(*observations: dict, batch_id: str = "B-1", connector: str = "TED_EU") -> dict:
    return {"batch_id": batch_id, "connector": connector, "retrieved_at": iso(NOW), "observations": list(observations)}


def crm(record_id: str = "CRM-0001", **kw: Any) -> dict:
    base = {"record_id": record_id, "record_type": "CRM_ACCOUNT", "buyer_name": "Stadtwerke Beispielstadt",
            "buyer_identifiers": {"vat": "DE999999999"}, "buyer_country": "DE", "buyer_type": "PUBLIC",
            "relationship_stage": "WARM", "recorded_at": iso(days(-5)), "service_categories": ["power_electronics"]}
    base.update(kw)
    return base


FX = [{"currency": "EUR", "rate_to_base": "1.08", "as_of": (NOW - timedelta(days=1)).date().isoformat(),
       "source": "ECB reference rate (test fixture)"},
      {"currency": "INR", "rate_to_base": "0.012", "as_of": (NOW - timedelta(days=1)).date().isoformat(),
       "source": "RBI reference rate (test fixture)"}]


def report_request(**kw: Any) -> dict:
    return {"tenant_id": "acme", "serviceable_categories": ["power_electronics"], "base_currency": "USD",
            "fx_rates": FX, **kw}


# ------------------------------------------------------------------------------------------------
class N1:
    def __init__(self, client: TestClient, app) -> None:
        self.c, self.app = client, app

    def h(self, who: Optional[str]) -> dict:
        return {"Authorization": f"Bearer {token(who)}"} if who else {}

    def post(self, path: str, who: Optional[str] = "ana", **kw):
        return self.c.post(path, headers=self.h(who), **kw)

    def get(self, path: str, who: Optional[str] = "ana", **kw):
        return self.c.get(path, headers=self.h(who), **kw)

    def source(self, source_id: str = "SRC-TED", tenant: str = "acme", permitted_use=MARKET_USES,
               approve: bool = True, **kw: Any) -> dict:
        admin, approver = ("sam", "rita") if tenant == "acme" else ("gadmin", "grights")
        r = self.post("/v1/sources/register", admin, json={"source_id": source_id, "tenant_id": tenant,
                                                           "publisher": "TED (Publications Office of the EU)",
                                                           "license": kw.pop("license", "CC-BY-4.0"),
                                                           "permitted_use": list(permitted_use), **kw})
        assert r.status_code == 201, r.text
        if approve:
            r = self.post(f"/v1/sources/{tenant}/{source_id}/approve", approver,
                          json={"decision": "APPROVED", "reason": "public procurement data, CC BY 4.0"})
            assert r.status_code == 200, r.text
        return r.json()

    def submit(self, *observations: dict, who: str = "ana", source_id: str = "SRC-TED", synthetic: bool = False,
               tenant: str = "acme", batch_id: str = "B-1", **kw: Any):
        body = {"tenant_id": tenant, "project_id": "PRJ-MKT-2027", "source_id": source_id,
                "batch": batch(*observations, batch_id=batch_id), "synthetic": synthetic, **kw}
        return self.post("/v1/n1/batches", who, json=body)

    def submit_ok(self, *observations: dict, **kw: Any) -> dict:
        r = self.submit(*observations, **kw)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["n0_decision"]["decision"] == "PASS", body["n0_decision"]["explanation"]
        return body

    def report(self, who: str = "ana", **kw: Any) -> dict:
        r = self.post("/v1/n1/reports", who, json=report_request(**kw))
        assert r.status_code == 200, r.text
        return r.json()

    def attest(self, evidence_id: str, who: str = "apr", status: str = "ATTESTED"):
        return self.post("/v1/n1/attestations", who, json={"evidence_id": evidence_id, "status": status,
                                                           "method": "OFFICIAL_PORTAL_CHECK",
                                                           "notes": "checked against the official notice"})


@pytest.fixture
def make_n1(tmp_path):
    counter = {"n": 0}

    def factory(n1_settings: Optional[N1Settings] = None, **kw: Any) -> N1:
        counter["n"] += 1
        d = tmp_path / f"app-{counter['n']}"
        overrides = {k: kw.pop(k) for k in list(kw) if k in Settings.model_fields}
        settings = Settings(database_url=f"sqlite:///{d}/n0n1.db", storage_root=str(d / "objects"), tokens={},
                            jwt_secret=SECRET, jwt_issuer=ISSUER, **overrides)
        app = create_app(settings, n1_settings=n1_settings or N1Settings(), **kw)
        return N1(TestClient(app), app)
    return factory


@pytest.fixture
def n1(make_n1) -> N1:
    return make_n1()


@pytest.fixture
def n1_src(n1) -> N1:
    n1.source()
    return n1


def deep(d: dict) -> dict:
    return copy.deepcopy(d)


# ------------------------------------------------------------------------------------------------
# N2 / N3
# ------------------------------------------------------------------------------------------------
@pytest.fixture
def n3(make_n1, tmp_path) -> N1:
    from n3_support import n3_settings
    return make_n1(n3_settings=n3_settings(tmp_path / "jobs"))


@pytest.fixture
def flow(n3):
    from n3_support import Flow
    return Flow(n3)


@pytest.fixture
def n4(make_n1, tmp_path) -> N1:
    from n3_support import n3_settings
    from app.n4.config import N4Settings
    return make_n1(n3_settings=n3_settings(tmp_path / "jobs"), n4_settings=N4Settings())


@pytest.fixture
def flow4(n4):
    from n4_support import N4Flow
    return N4Flow(n4)
