"""End-to-end ingestion API tests, on SQLite."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["ATHENA_DATABASE_URL"] = "sqlite+aiosqlite:///./test_ingest.db"
os.environ.setdefault("ATHENA_ENVIRONMENT", "test")
os.environ.setdefault("ATHENA_DISPATCH_ASYNC", "false")

API = "/api/v1"

COMPLETE = """AOD4184A MOSFET
Drain-Source Voltage V(BR)DSS = 40 V
Thermal Resistance, Junction-to-Case RthJC 1.8 C/W
Junction Temperature TJ (max) 175 C
Creepage distance: 3.2 mm
Clearance distance: 2.6 mm
Working voltage 250 V
Lead time 12 weeks
Rated capacity 2000 mAh
Continuous current ID 50 A
"""

NO_THERMAL = """ACME-CONN-8P Connector
8 position, 1.0 mm pitch, gold flash, RoHS.
Creepage distance: 1.2 mm
Clearance distance: 0.9 mm
Working voltage 50 V
Operating temperature 85 C
"""


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.db import Base, create_all, dispose_engine, get_engine
    from app.main import app

    async def _setup():
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await create_all()
        await dispose_engine()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_setup())
    finally:
        loop.close()

    with TestClient(app) as c:
        yield c

    db = ROOT / "test_ingest.db"
    if db.exists():
        try:
            db.unlink()
        except OSError:
            pass


def _ingest(client, text, part_no, **meta):
    body = {"text": text, "filename": f"{part_no}.txt",
            "metadata": {"ref_part_no": part_no, **meta}}
    return client.post(f"{API}/datasheets/ingest-text", json=body)


class TestHealth:
    def test_health_reports_the_policy(self, client):
        caps = client.get("/health").json()["capabilities"]
        assert caps["extraction"]["refuse_defaults"] is True
        assert caps["extraction"]["hold_incomplete_safety"] is True
        assert caps["retrieval"]["backend"]

    def test_the_parameter_schema_is_published(self, client):
        body = client.get(f"{API}/datasheets/schema/parameters").json()
        assert len(body["parameters"]) >= 8
        assert "never defaulted" in body["policy"]


class TestIngest:
    def test_a_complete_datasheet_is_published(self, client):
        r = _ingest(client, COMPLETE, "AOD4184A", supplier="Alpha & Omega")
        assert r.status_code == 201
        body = r.json()
        assert body["status"] in ("PUBLISHED", "PARSED")
        keys = {e["key"] for e in body["extracted"]}
        assert {"rth_jc_k_per_w", "creepage_mm", "v_ds_max_v"} <= keys

    def test_re_uploading_the_same_bytes_returns_the_stored_extraction(self, client):
        """Two extractions of one file that disagree is unresolvable later."""
        first = _ingest(client, COMPLETE, "AOD4184A", supplier="Alpha & Omega").json()
        second = _ingest(client, COMPLETE, "AOD4184A", supplier="Alpha & Omega").json()
        assert second["duplicate"] is True
        assert second["document_id"] == first["document_id"]

    def test_a_missing_safety_parameter_holds_the_document(self, client):
        r = _ingest(client, NO_THERMAL, "ACME-CONN-8P",
                    expect=["rth_jc_k_per_w", "creepage_mm"])
        body = r.json()
        assert body["status"] == "HELD"
        assert body["missing_report"]["safety_relevant_missing"] == ["rth_jc_k_per_w"]
        assert not any(e["key"] == "rth_jc_k_per_w" for e in body["extracted"]), (
            "a value was invented for a parameter that is not in the document")

    def test_a_held_document_cannot_be_published(self, client):
        doc = _ingest(client, NO_THERMAL + "\n(rev 2)", "ACME-CONN-8P-R2",
                      expect=["rth_jc_k_per_w"]).json()
        r = client.post(f"{API}/datasheets/{doc['document_id']}/publish",
                        json={"reason": "We need this for the thermal model."})
        assert r.status_code == 409
        assert "rth_jc_k_per_w" in r.json()["detail"]

    def test_supplying_the_value_manually_unblocks_it(self, client):
        doc = _ingest(client, NO_THERMAL + "\n(rev 3)", "ACME-CONN-8P-R3",
                      expect=["rth_jc_k_per_w"]).json()
        did = doc["document_id"]
        r = client.post(f"{API}/datasheets/{did}/specs", json={
            "key": "rth_jc_k_per_w", "value": 42.0, "unit": "K/W",
            "reason": "Read from the supplier mechanical drawing rev C, table 4."})
        assert r.status_code == 201
        assert r.json()["confidence"] == "MANUAL", (
            "a value a person typed must not carry an extraction confidence")
        r = client.post(f"{API}/datasheets/{did}/publish",
                        json={"reason": "Thermal resistance supplied from drawing."})
        assert r.status_code == 200
        assert r.json()["status"] == "PUBLISHED"

    def test_a_manual_value_needs_a_reason(self, client):
        doc = _ingest(client, NO_THERMAL + "\n(rev 4)", "ACME-CONN-8P-R4",
                      expect=["rth_jc_k_per_w"]).json()
        r = client.post(f"{API}/datasheets/{doc['document_id']}/specs", json={
            "key": "rth_jc_k_per_w", "value": 42.0, "reason": "because"})
        assert r.status_code == 422

    def test_an_implausible_manual_value_is_refused(self, client):
        doc = _ingest(client, NO_THERMAL + "\n(rev 5)", "ACME-CONN-8P-R5",
                      expect=["rth_jc_k_per_w"]).json()
        r = client.post(f"{API}/datasheets/{doc['document_id']}/specs", json={
            "key": "rth_jc_k_per_w", "value": 5000.0,
            "reason": "Typed in from a phone call with the supplier."})
        assert r.status_code == 422
        assert "plausible range" in r.json()["detail"]

    def test_an_unknown_parameter_key_is_refused(self, client):
        doc = _ingest(client, COMPLETE, "AOD4184A").json()
        r = client.post(f"{API}/datasheets/{doc['document_id']}/specs", json={
            "key": "made_up_parameter", "value": 1.0,
            "reason": "Trying to add something the schema does not know."})
        assert r.status_code == 422

    def test_the_held_queue_is_listable(self, client):
        _ingest(client, NO_THERMAL + "\n(rev 6)", "ACME-CONN-8P-R6",
                expect=["rth_jc_k_per_w"])
        body = client.get(f"{API}/datasheets/held").json()
        assert body["count"] >= 1
        assert all("missing" in d for d in body["documents"])


class TestPartParameters:
    def test_parameters_carry_provenance(self, client):
        _ingest(client, COMPLETE, "AOD4184A", supplier="Alpha & Omega")
        body = client.get(f"{API}/datasheets/parts/AOD4184A/parameters").json()
        rth = body["parameters"]["rth_jc_k_per_w"]
        assert rth["value"] == 1.8
        assert rth["evidence"]
        assert rth["document_id"]

    def test_conflicting_sources_are_surfaced_not_resolved(self, client):
        """Two datasheets disagreeing must not silently collapse to one value."""
        _ingest(client, COMPLETE, "CONFLICT-PART")
        variant = COMPLETE.replace("RthJC  1.8 C/W", "RthJC  2.9 C/W").replace(
            "RthJC 1.8 C/W", "RthJC 2.9 C/W")
        _ingest(client, variant + "\n(distributor sheet)", "CONFLICT-PART")
        body = client.get(f"{API}/datasheets/parts/CONFLICT-PART/parameters").json()
        conflicts = {c["key"] for c in body["conflicts"]}
        assert "rth_jc_k_per_w" in conflicts
        conflict = next(c for c in body["conflicts"]
                        if c["key"] == "rth_jc_k_per_w")
        assert len(conflict["values"]) == 2
        assert "engineering decision" in conflict["note"]

    def test_absent_safety_parameters_are_listed(self, client):
        body = client.get(
            f"{API}/datasheets/parts/ACME-CONN-8P/parameters").json()
        assert "rth_jc_k_per_w" in body["safety_parameters_absent"]


class TestSearch:
    def test_retrieval_returns_the_relevant_passage(self, client):
        _ingest(client, COMPLETE, "AOD4184A")
        body = client.get(f"{API}/datasheets/search/chunks",
                          params={"q": "junction to case thermal resistance"}).json()
        assert body["results"]
        assert any("RthJC" in r["text"] or "Thermal" in r["text"]
                   for r in body["results"])

    def test_the_backend_is_declared(self, client):
        body = client.get(f"{API}/datasheets/search/chunks",
                          params={"q": "thermal"}).json()
        assert body["backend"]
        assert "lexical" in body["stats"]["note"].lower()
