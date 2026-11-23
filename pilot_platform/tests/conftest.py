import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
GOLD = json.loads((Path(__file__).parent.parent / "data" / "gold_rfq_cases.json").read_text())["cases"]
RFQ_TEXT = GOLD[0]["text"]


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(database_url="sqlite://", password_iterations=1000, vendor_outbox_dir=str(tmp_path / "outbox"))
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def app(tmp_path):
    return create_app(make_settings(tmp_path))


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth(client):
    cache = {}

    def _auth(user: str) -> dict:
        if user not in cache:
            r = client.post("/auth/login", json={"username": user, "password": f"{user}-demo"})
            assert r.status_code == 200, r.text
            cache[user] = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return cache[user]

    return _auth


@pytest.fixture
def db(app):
    session = app.state.sessionmaker()
    yield session
    session.close()


@pytest.fixture
def pid(client, auth):
    return client.get("/programmes", headers=auth("pm")).json()[0]["id"]


def pipeline_input(key="RFQ-001", bids=2, justification=None) -> dict:
    return {
        "rfq_key": key, "customer": "Acme Logistics", "rfq_text": RFQ_TEXT,
        "design": {
            "eda_tool": "KiCad 8", "files": ["tracker.kicad_pcb"], "author": "engineer", "reviewed_by": "engineer2",
            "erc_passed": True, "drc_passed": True,
            "simulations": [
                {"type": "thermal", "assumptions": ["still air", "theta-JA from datasheet"],
                 "inputs": {"power_w": 0.5, "theta_ja_c_per_w": 40, "ambient_c": 60, "tj_max_c": 125}},
                {"type": "spice", "assumptions": ["nominal component values"], "result": {"passed": True}},
            ],
        },
        "pfmea": [
            {"failure_mode": "battery reverse polarity", "effect": "board damage", "severity": 9, "occurrence": 2,
             "detection": 3, "action": "add reverse-polarity MOSFET"},
            {"failure_mode": "antenna detuning", "effect": "range loss", "severity": 5, "occurrence": 4, "detection": 4},
        ],
        "sourcing": {
            "currency": "USD", "as_of": "2026-11-01", "single_source_justification": justification,
            "bom": [
                {"mpn": "nrf52840-qiaa-r", "manufacturer": "Nordic", "qty_per_unit": 1, "refdes": ["U1"]},
                {"mpn": "GRM155R71C104KA88D", "manufacturer": "Murata", "qty_per_unit": 10,
                 "refdes": [f"C{i}" for i in range(1, 11)]},
            ],
            "bids": [
                {"supplier": "DistA", "currency": "USD", "valid_until": "2026-12-31", "lead_time_days": 21,
                 "unit_prices": {"NRF52840-QIAA-R": "3.10", "GRM155R71C104KA88D": "0.01"}},
                {"supplier": "DistB", "currency": "USD", "valid_until": "2026-12-31", "lead_time_days": 14,
                 "unit_prices": {"NRF52840-QIAA-R": "3.25", "GRM155R71C104KA88D": "0.008"}},
            ][:bids],
        },
        "commercial": {"build_qty": 5000, "nre": "15000", "labor_hours": 120, "labor_rate_per_hour": "45",
                       "ai_cost": "12.40", "test_cost_per_unit": "0.80", "target_margin": "0.22",
                       "annual_finance_rate": "0.11", "finance_days": 75},
    }
