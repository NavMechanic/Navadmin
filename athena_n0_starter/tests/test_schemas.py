"""Contracts: the eight schema families are published as JSON Schema 2020-12 with the specified
fields, the committed files match the models, API outputs conform to them, and the Alembic
migration reproduces the ORM schema including the append-only triggers."""
from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, text

from app.schema_export import SCHEMA_DIR, expected_files
from conftest import ROOT, load_example

FAMILY_FIELDS = {
    "SourceRecord.v1": ["source_id", "tenant_id", "publisher", "license", "permitted_use", "approval_status",
                        "approved_by", "approved_at"],
    "Artifact.v1": ["tenant_id", "project_id", "artifact_id", "schema_version", "artifact_type", "source_id",
                    "processing_purpose", "release_stage", "revision", "synthetic", "source_sha256", "file_refs",
                    "payload"],
    "Requirement.v1": ["req_id", "parameter", "value", "unit", "tolerance", "operating_conditions", "value_origin",
                       "evidence_snippets"],
    "ModelRun.v1": ["run_id", "model_name", "model_version", "prompt_revision", "input_hash", "output_payload",
                    "token_usage", "estimated_cost_usd"],
    "VendorWorkPackage.v1": ["work_package_id", "vendor_id", "connector_type", "approved_input_ids", "budget_cap",
                             "expected_outputs"],
    "VendorResult.v1": ["work_package_id", "native_file_uris", "result_manifest", "checksum_sha256", "tool_version",
                        "warnings"],
    "GatewayDecision.v1": ["decision", "violated_rules", "missing_fields", "rights_decision", "explanation",
                           "allowed_destinations", "validator_version"],
    "ApprovalEvent.v1": ["approval_id", "artifact_id", "exact_revision", "approver_id", "approver_role", "decision",
                         "reason", "timestamp_utc"],
}
ENUMS = {
    ("Requirement.v1", "value_origin"): {"CUSTOMER", "MEASURED", "DERIVED", "MODEL_EXTRACTED"},
    ("GatewayDecision.v1", "decision"): {"PASS", "REWORK", "BLOCK", "REVIEW"},
    ("VendorWorkPackage.v1", "connector_type"): {"REST_GRAPHQL", "CLI_SDK", "FILE_PORTAL", "SCPI_VISA", "MES_ERP",
                                                 "MQTT_OPCUA"},
    ("Artifact.v1", "artifact_type"): {"RFQ", "DATASHEET", "EDA_PACKAGE", "SUPPLIER_QUOTE", "TEST_MEASUREMENT",
                                       "MARKET_FEED", "FINANCE_RECORD", "TELEMETRY", "MODEL_OUTPUT", "MARKET_BATCH",
                                       "BUYER_RECORD", "COMMERCIAL_SHORTLIST", "SAM_EVIDENCE"},
}


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_schema(name))


def test_committed_schemas_match_the_models():
    drift = [p for p, content in expected_files().items()
             if not (SCHEMA_DIR / p).exists() or (SCHEMA_DIR / p).read_text() != content]
    assert drift == [], f"run `python -m app.schema_export`; stale: {drift}"


@pytest.mark.parametrize("name", sorted(FAMILY_FIELDS))
def test_schema_family_is_draft_2020_12_with_specified_fields(name):
    s = _schema(name)
    Draft202012Validator.check_schema(s)
    assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema" and s["title"] == name
    assert s.get("additionalProperties") is False
    missing = [f for f in FAMILY_FIELDS[name] if f not in s["properties"]]
    assert missing == []


@pytest.mark.parametrize("key", sorted(ENUMS))
def test_controlled_vocabularies(key):
    name, prop = key
    assert set(_schema(name)["properties"][prop]["enum"]) == ENUMS[key]


@pytest.mark.parametrize("example", ["valid_rfq.json", "invalid_rfq.json"])
def test_examples_conform_to_published_schemas(example):
    a = load_example(example)
    assert list(_validator("Artifact.v1").iter_errors(a)) == []
    payload = Draft202012Validator(json.loads((SCHEMA_DIR / "payloads" / "RFQPayload.v1.schema.json").read_text()))
    assert list(payload.iter_errors(a["payload"])) == []          # invalid_rfq is schema-valid; its defects are semantic


def test_api_outputs_conform_to_their_schemas(n0_src):
    res = n0_src.evaluate(load_example("valid_rfq.json"))
    for name, doc in (("GatewayDecision.v1", res["decision"]), ("ReleaseManifest.v1", res["release_manifest"]),
                      ("Artifact.v1", res["canonical_artifact"]), ("EvaluationResult.v1", res)):
        assert list(_validator(name).iter_errors(doc)) == [], name
    for r in res["canonical_artifact"]["payload"]["requirements"]:
        assert list(_validator("Requirement.v1").iter_errors(r)) == []
    ev = n0_src.approve_release("RFQ-2026-0042").json()["approval_event"]
    assert list(_validator("ApprovalEvent.v1").iter_errors(ev)) == []
    src = n0_src.get("/v1/sources/acme/SRC-ACME-RFQ-PORTAL").json()
    assert list(_validator("SourceRecord.v1").iter_errors(src)) == []


def test_openapi_documents_every_endpoint():
    spec = json.loads((SCHEMA_DIR / "openapi.json").read_text())
    expected = {("post", "/v1/sources/register"), ("post", "/v1/sources/{tenant_id}/{source_id}/approve"),
                ("post", "/v1/artifacts/upload"), ("post", "/v1/gateway/evaluate"), ("get", "/health"),
                ("post", "/v1/extraction/run"), ("get", "/v1/artifacts/{artifact_id}/lineage"),
                ("post", "/v1/handoffs/release"), ("post", "/v1/gateway/feedback"), ("post", "/v1/approvals"),
                ("post", "/v1/vendor/work-packages"), ("post", "/v1/vendor/results"),
                ("get", "/v1/artifacts/{artifact_id}"), ("get", "/v1/audit/verify")}
    have = {(m, p) for p, ops in spec["paths"].items() for m in ops}
    assert expected <= have


def test_alembic_migration_matches_models_and_enforces_append_only(tmp_path, monkeypatch):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    from app.db import Base

    url = f"sqlite:///{tmp_path}/migrated.db"
    monkeypatch.setenv("N0_DATABASE_URL", url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["configure_logging"] = False
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
        triggers = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert {"audit_events_no_update", "decisions_no_delete", "lineage_edges_no_update"} <= triggers
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO lineage_edges (tenant_id, source, target, relation, attrs, created_at) "
                          "VALUES ('t', 'a', 'b', 'R', '{}', '2026-11-24')"))
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE lineage_edges SET relation='X'"))
    command.downgrade(cfg, "base")
