"""Durable persistence and the quote vault: append-only N5 / N6 / N7 tables, state that survives a
restart, write-once SHA-256-verified quote files (tampering after approval stops the handoff), and
the Alembic chain (0001 → 0005)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.n1.config import N1Settings
from app.n4.config import N4Settings
from app.n5.config import N5Settings
from app.n5.db import N5_APPEND_ONLY_TABLES
from app.n6.service import N6_APPEND_ONLY_TABLES
from app.n7.service import N7_APPEND_ONLY_TABLES
from conftest import ISSUER, N1, SECRET
from n3_support import ROOT, n3_settings
from n5_support import MURATA, N5Flow

TABLES = N5_APPEND_ONLY_TABLES + N6_APPEND_ONLY_TABLES + N7_APPEND_ONLY_TABLES


def _app(tmp_path) -> N5Flow:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/n5.db", storage_root=str(tmp_path / "objects"), tokens={},
                        jwt_secret=SECRET, jwt_issuer=ISSUER)
    app = create_app(settings, n1_settings=N1Settings(), n3_settings=n3_settings(tmp_path / "jobs"),
                     n4_settings=N4Settings(), n5_settings=N5Settings())
    return N5Flow(N1(TestClient(app), app))


def test_every_n5_n6_n7_table_is_append_only(tmp_path):
    flow = _app(tmp_path)
    intake, rep, h = flow.handed_off()
    assert flow.fx().status_code == 201
    flow.c.post(f"/v1/n5/bom-intakes/{intake['intake_id']}/lookups", "pan", json={"connectors": ["DIGIKEY"]})
    flow.c.post("/v1/n5/eco-proposals", "pan", json={
        "bom_intake_id": intake["intake_id"], "line": flow.L[MURATA], "proposed_mpn": "CL32B475KCJSNNE",
        "proposed_manufacturer": "Samsung Electro-Mechanics", "reason": "second source for the X7R capacitor",
        "equivalence_evidence": [{"kind": "DATASHEET", "reference": "datasheet", "summary": "same rating"}],
        "parametric_comparison": [{"parameter": "rated_voltage", "approved_value": "100", "proposed_value": "100"}]})
    pk = {x["target"]: x for x in h["packages"]}
    for node, who in (("n6", "co"), ("n7", "fo")):
        target = node.upper()
        assert flow.c.post(f"/v1/{node}/intake", who, json={"artifact_id": pk[target]["artifact_id"],
                                                            "revision": pk[target]["revision"]}).status_code == 201
    assert flow.c.post("/v1/sources/acme/SRC-QUOTES/approve", "rita",
                       json={"decision": "REVOKED", "reason": "persistence test"}).status_code == 200
    engine = flow.c.app.state.db.engine
    for table in TABLES:
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() > 0, table
        for stmt in (f"UPDATE {table} SET tenant_id = 'x'", f"DELETE FROM {table}"):
            with engine.connect() as conn, pytest.raises(Exception, match="append-only"):
                conn.execute(text(stmt))


def test_state_survives_a_restart_and_the_handoff_continues(tmp_path):
    first = _app(tmp_path)
    intake, rep = first.sourced()
    again = _app(tmp_path)                                            # same database and object store
    again.L = first.L
    view = again.c.get(f"/v1/n5/reports/{rep['report_id']}", "aud").json()
    assert view["report_sha256"] == rep["report_sha256"] and view["indicative_total"] == "244.00"
    assert again.n5_review(rep).status_code == 201
    assert again.n5_handoff(rep).status_code == 201
    assert again.c.get("/v1/audit/verify", "aud").json()["audit"]["ok"]


def test_tampering_with_the_quote_vault_after_approval_stops_the_handoff(tmp_path):
    flow = _app(tmp_path)
    flow.bom_released()
    flow.ems_supplier()
    intake = flow.n5_intake()
    q = flow.ems_quote(intake).json()
    rep = flow.n5_report(intake)
    assert rep["decision"] == "PASS", rep["findings"]
    assert flow.n5_review(rep).status_code == 201
    doc = next(f for f in q["files"] if f["role"] == "QUOTE_DOCUMENT")
    path = Path(tmp_path, "objects", "quarantine", "acme", doc["sha256"])
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b"% price edited after approval\n")
    r = flow.n5_handoff(rep)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "QUOTE_HASH_MISMATCH"
    assert flow.c.get("/v1/artifacts/PRC-PSU-120W-N7", "aud").status_code == 404


def test_alembic_migrations_build_the_full_schema_with_triggers(tmp_path):
    db = tmp_path / "mig.db"
    env = {**os.environ, "N0_DATABASE_URL": f"sqlite:///{db}"}
    for cmd in (["upgrade", "head"], ["check"], ["downgrade", "0004"], ["upgrade", "head"]):
        out = subprocess.run([sys.executable, "-m", "alembic", *cmd], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
    con = sqlite3.connect(db)
    triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {f"{t}_no_update" for t in TABLES} <= triggers and {f"{t}_no_delete" for t in TABLES} <= triggers
    assert set(Base.metadata.tables) <= {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_postgresql_ddl_renders_for_every_n5_n6_n7_table():
    for name in TABLES:
        ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=postgresql.dialect()))
        assert ddl.startswith(f"\nCREATE TABLE {name}") and "JSONB" in ddl
