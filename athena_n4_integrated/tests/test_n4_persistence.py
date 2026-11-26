"""Durable persistence and the evidence vault: append-only N4 / N8 tables, state that survives a
restart, write-once SHA-256-verified evidence (tampering after approval stops the release), and the
Alembic chain."""
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
from app.n4.db import N4_APPEND_ONLY_TABLES
from app.n8.service import N8_APPEND_ONLY_TABLES
from conftest import ISSUER, N1, SECRET
from n3_support import ROOT, n3_settings
from n4_support import N4Flow
from test_n4_ai import FakeLLM, SOP_TEXT, _script

TABLES = N4_APPEND_ONLY_TABLES + N8_APPEND_ONLY_TABLES


def _app(tmp_path, provider=None) -> N4Flow:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/n4.db", storage_root=str(tmp_path / "objects"), tokens={},
                        jwt_secret=SECRET, jwt_issuer=ISSUER)
    app = create_app(settings, n1_settings=N1Settings(), n3_settings=n3_settings(tmp_path / "jobs"),
                     n4_settings=N4Settings(), n4_provider=provider)
    return N4Flow(N1(TestClient(app), app))


def test_every_n4_and_n8_table_is_append_only(tmp_path):
    flow = _app(tmp_path, FakeLLM(_script))
    i, p, a = flow.verified()
    pid = p["plan_id"]
    flow.c.post("/v1/n4/corpus", "qr", json={"doc_id": "SOP-HIPOT", "kind": "SOP", "title": "Hipot",
                                             "source_id": "SRC-KB", "text": SOP_TEXT})
    assert flow.c.post(f"/v1/n4/plans/{pid}/suggestions", "te", json={"kind": "RCA_HYPOTHESIS",
                                                                      "question": "hipot failure"}).status_code == 201
    flow.c.post(f"/v1/n4/plans/{pid}/reliability", "te", json={"units": [{"unit_serial": "PSU-0001", "hours": 10}]})
    flow.c.post("/v1/n4/capas", "te", json={"capa_id": "CAPA-1", "plan_id": pid, "title": "stencil wear",
                                            "pfmea_entry_ids": ["FM-04"], "root_cause": "wear",
                                            "corrective_action": "counter", "owner": "te"})
    a = flow.n4_assess(pid)
    flow.n4_review(a)
    rel = flow.n4_release(pid).json()
    pk = {x["target"]: x for x in rel["packages"]}
    assert flow.c.post("/v1/n8/intake", "lo", json={"artifact_id": pk["N8"]["artifact_id"],
                                                    "revision": pk["N8"]["revision"]}).status_code == 201
    engine = flow.c.app.state.db.engine
    for table in TABLES:
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() > 0, table
        for stmt in (f"UPDATE {table} SET tenant_id = 'x'", f"DELETE FROM {table}"):
            with engine.connect() as conn, pytest.raises(Exception, match="append-only"):
                conn.execute(text(stmt))


def test_state_survives_a_restart_and_the_release_continues(tmp_path):
    first = _app(tmp_path)
    i, p, a = first.verified()
    again = _app(tmp_path)                                                   # same database and object store
    view = again.c.get(f"/v1/n4/plans/{p['plan_id']}", "aud").json()
    assert view["plan"]["plan_sha256"] == p["plan_sha256"] and len(view["evidence"]) == 10
    assert again.c.get(f"/v1/n4/assessments/{a['assessment_id']}", "aud").json()["evidence_sha256"] == \
        a["evidence_sha256"]
    assert again.n4_review(a).status_code == 201
    assert again.n4_release(p["plan_id"]).status_code == 201
    assert again.c.get("/v1/audit/verify", "aud").json()["audit"]["ok"]


def test_tampering_with_the_vault_after_approval_stops_the_release(tmp_path):
    flow = _app(tmp_path)
    i, p, a = flow.verified()
    assert flow.n4_review(a).status_code == 201
    ev = next(e for e in flow.c.get(f"/v1/n4/plans/{p['plan_id']}", "aud").json()["evidence"]
              if e["adapter"] == "ACCREDITED_LAB")
    pdf = next(f for f in ev["files"] if f["role"] == "LAB_REPORT")
    path = Path(tmp_path, "objects", "quarantine", "acme", pdf["sha256"])
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b"PCB Rev B", b"PCB Rev C"))
    r = flow.n4_release(p["plan_id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "EVIDENCE_HASH_MISMATCH"
    assert flow.c.get("/v1/artifacts/VER-PSU-120W-N8", "aud").status_code == 404


def test_alembic_migrations_build_the_full_schema_with_triggers(tmp_path):
    db = tmp_path / "mig.db"
    env = {**os.environ, "N0_DATABASE_URL": f"sqlite:///{db}"}
    for cmd in (["upgrade", "head"], ["check"], ["downgrade", "0003"], ["upgrade", "head"]):
        out = subprocess.run([sys.executable, "-m", "alembic", *cmd], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
    con = sqlite3.connect(db)
    triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {f"{t}_no_update" for t in TABLES} <= triggers and {f"{t}_no_delete" for t in TABLES} <= triggers
    assert set(Base.metadata.tables) <= {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_postgresql_ddl_renders_for_every_n4_and_n8_table():
    for name in TABLES:
        ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=postgresql.dialect()))
        assert ddl.startswith(f"\nCREATE TABLE {name}") and "JSONB" in ddl
