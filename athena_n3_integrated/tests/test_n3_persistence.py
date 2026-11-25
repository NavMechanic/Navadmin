"""Durable persistence: append-only N2 / N3 tables (database triggers), state that survives a restart,
immutable SHA-256-verified object storage, and the Alembic migration chain."""
from __future__ import annotations

import os
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
from app.n3.db import N3_APPEND_ONLY_TABLES
from app.storage import ImmutableObjectError
from conftest import ISSUER, N1, SECRET
from n3_support import ROOT, Flow, design_files, n3_settings, sha


def _app(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/n3.db", storage_root=str(tmp_path / "objects"), tokens={},
                        jwt_secret=SECRET, jwt_issuer=ISSUER)
    app = create_app(settings, n1_settings=N1Settings(), n3_settings=n3_settings(tmp_path / "jobs"))
    return Flow(N1(TestClient(app), app))


@pytest.mark.parametrize("table", ["n2_prds", "n2_prd_releases"] + list(N3_APPEND_ONLY_TABLES))
def test_tables_are_append_only(flow, table):
    b = flow.ready()
    a = flow.work_package(flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"])
    flow.review(a["assessment_id"])
    flow.release(b["baseline_id"], "circuit_simulation")
    flow.onboard()
    flow.c.post("/v1/n3/assessments/" + a["assessment_id"] + "/actions", "de", json={
        "assessment_id": a["assessment_id"], "currency": "USD", "candidates": [
            {"action_id": "A-1", "action_type": "FIX_IN_HOUSE", "addresses": ["X"], "cost": "1",
             "risk_reduction_value": "1"}]})
    flow.c.post("/v1/n3/feedback", "de", json={"target_kind": "ASSESSMENT", "target_id": a["assessment_id"],
                                               "label": "CORRECT", "reason": "fine"})
    engine = flow.c.app.state.db.engine
    with engine.connect() as conn:
        assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() > 0
    for stmt in (f"UPDATE {table} SET tenant_id = 'x'", f"DELETE FROM {table}"):
        with engine.connect() as conn, pytest.raises(Exception, match="append-only"):
            conn.execute(text(stmt))


def test_state_survives_a_restart_and_work_continues(tmp_path):
    first = _app(tmp_path)
    b = first.ready()
    a = first.work_package(first.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"])
    first.c.app.state.n3.worker.shutdown()
    again = _app(tmp_path)                                                       # same database + object store
    view = again.c.get(f"/v1/n3/baselines/{b['baseline_id']}", "de").json()
    assert view["baseline"]["baseline_sha256"] == b["baseline_sha256"] and len(view["work_packages"]) == 1
    got = again.c.get(f"/v1/n3/assessments/{a['assessment_id']}", "aud").json()
    assert got["assessment_sha256"] == a["assessment_sha256"] and got["decision"] == "PASS"
    assert again.review(a["assessment_id"]).status_code == 201
    r = again.release(b["baseline_id"], "circuit_simulation")
    assert r.status_code == 201 and r.json()["status"] == "RELEASED"
    assert again.c.get("/v1/audit/verify", "aud").json()["audit"]["ok"]


def test_object_storage_is_immutable_and_verified_on_read(flow):
    b = flow.ready()
    wp = flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"]
    a = flow.work_package(wp)
    net = design_files()["psu_buck.cir"]
    store = flow.c.app.state.store
    key = store.key_for("acme", sha(net))
    assert store.get("quarantine", key) == net
    with pytest.raises(ImmutableObjectError):
        store.put_immutable("quarantine", key, net + b"* changed\n")
    path = Path(flow.c.app.state.settings.storage_root) / "quarantine" / key
    os.chmod(path, 0o644)
    path.write_bytes(net.replace(b"22u", b"47u"))                               # out-of-band tampering
    r = flow.c.post(f"/v1/n3/work-packages/{wp['work_package_id']}/assess", "de")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STORED_FILE_CORRUPT"
    assert a["files"][0]["sha256"] == sha(net)


def test_alembic_migrations_build_the_full_schema_with_triggers(tmp_path):
    db = tmp_path / "mig.db"
    env = {**os.environ, "N0_DATABASE_URL": f"sqlite:///{db}"}
    for cmd in (["upgrade", "head"], ["check"], ["downgrade", "0002"], ["upgrade", "head"]):
        out = subprocess.run([sys.executable, "-m", "alembic", *cmd], cwd=ROOT, env=env, capture_output=True,
                             text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
    import sqlite3
    con = sqlite3.connect(db)
    triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {f"{t}_no_update" for t in N3_APPEND_ONLY_TABLES} <= triggers
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(Base.metadata.tables) <= tables


def test_postgresql_ddl_renders_for_every_n3_table():
    for name in N3_APPEND_ONLY_TABLES:
        ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=postgresql.dialect()))
        assert ddl.startswith(f"\nCREATE TABLE {name}") and "JSONB" in ddl
