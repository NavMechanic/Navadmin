"""Contracts and migrations: committed schemas match the models, N1 schemas are valid 2020-12 with
the governance fields, the API conforms, and Alembic 0001+0002 reproduce the ORM with triggers."""
from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, text

from app.schema_export import SCHEMA_DIR, expected_files
from conftest import ROOT, award


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())


def test_committed_schemas_match_the_models():
    drift = [p for p, c in expected_files().items() if not (SCHEMA_DIR / p).exists() or (SCHEMA_DIR / p).read_text() != c]
    assert drift == [], f"run `python -m app.schema_export`; stale: {drift}"


def test_n1_schemas_encode_the_invariants():
    rep, opp, opt = _schema("n1_market_report"), _schema("n1_opportunity_assessment"), \
        _schema("n1_action_optimization_result")
    for s in (rep, opp, opt):
        Draft202012Validator.check_schema(s)
        assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert opp["properties"]["calibrated"]["const"] is False and opp["properties"]["win_probability"]["type"] == "null"
    assert opp["properties"]["score_kind"]["const"] == "HEURISTIC_PRIORITIZATION"
    assert opt["properties"]["executed"]["const"] is False
    sam = rep["$defs"]["SamSummary"]["properties"]
    assert {"verified_real_sam", "hypothetical_addressable_spend", "indicative_spend"} <= set(sam)
    obs = _schema("n1_market_observation")
    assert set(obs["properties"]["observation_type"]["enum"]) >= {"TENDER_NOTICE", "CONTRACT_AWARD", "TRADE_STATISTIC"}


def test_api_outputs_conform(n1_src):
    n1_src.submit_ok(award())
    n1_src.attest("TED-2026-000202")
    rep = n1_src.report()
    assert list(Draft202012Validator(_schema("n1_market_report")).iter_errors(rep)) == []
    acc = n1_src.get(f"/v1/n1/buyers/{rep['opportunities'][0]['account_id']}", "aud").json()["latest"]
    assert list(Draft202012Validator(_schema("n1_buyer_account")).iter_errors(acc)) == []


def test_openapi_lists_the_n1_endpoints():
    spec = json.loads((SCHEMA_DIR / "openapi.json").read_text())
    have = {(m, p) for p, ops in spec["paths"].items() for m in ops}
    assert {("post", "/v1/n1/batches"), ("post", "/v1/n1/intake"), ("post", "/v1/n1/reports"),
            ("post", "/v1/n1/reports/{report_id}/handoff"), ("post", "/v1/n1/attestations"),
            ("post", "/v1/n1/optimize"), ("post", "/v1/n1/feedback"), ("post", "/v1/n1/slm/extract"),
            ("post", "/v1/n1/connectors/{name}/preview"), ("post", "/v1/gateway/evaluate"),
            ("post", "/v1/handoffs/release")} <= have


def test_alembic_chain_matches_models_and_enforces_append_only(tmp_path, monkeypatch):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    from app.db import Base

    url = f"sqlite:///{tmp_path}/m.db"
    monkeypatch.setenv("N0_DATABASE_URL", url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["configure_logging"] = False
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
        triggers = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert {"n1_reports_no_update", "n1_observations_no_delete", "audit_events_no_update"} <= triggers
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO n1_feedback_cases VALUES ('C1','t','EVIDENCE','e','OTHER',1,'{}','2026-11-25')"))
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE n1_feedback_cases SET label='OUTCOME_WON'"))
    command.downgrade(cfg, "base")
