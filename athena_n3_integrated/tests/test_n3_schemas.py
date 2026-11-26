"""Published contracts: the 13 N3 JSON Schemas (plus job / vendor / routing records and bom_item.v1),
the N2 schemas and the combined N0_N1_N2_N3_OpenAPI.json, all generated from the Pydantic models."""
from __future__ import annotations

import json

import jsonschema
import pytest

from app.n3.models import N3_EXTRA_SCHEMAS, N3_SCHEMAS
from app.schema_export import expected_files
from n3_support import ROOT

SCHEMAS = ROOT / "schemas"
THIRTEEN = ["n3_design_baseline", "n3_work_package", "n3_work_package_request", "n3_vendor_delivery",
            "n3_file_manifest", "n3_file_evidence", "n3_finding", "n3_assessment", "n3_action", "n3_action_request",
            "n3_review_request", "n3_release", "n3_feedback"]


def test_thirteen_n3_schema_families_are_published():
    assert sorted(N3_SCHEMAS) == sorted(THIRTEEN)
    for name in THIRTEEN + list(N3_EXTRA_SCHEMAS) + ["n2_prd_release", "n2_prd_record"]:
        schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_committed_schemas_and_openapi_match_the_models():
    drift = [p for p, c in expected_files().items() if (SCHEMAS / p).read_text() != c]
    assert drift == [], f"run `python -m app.schema_export`: {drift}"


def test_openapi_covers_every_node():
    spec = json.loads((SCHEMAS / "N0_N1_N2_N3_OpenAPI.json").read_text())
    paths = set(spec["paths"])
    assert {"/v1/gateway/evaluate", "/v1/n1/reports", "/v1/n2/prds", "/v1/n3/baselines",
            "/v1/n3/work-packages/{wp_id}/deliveries", "/v1/n3/work-packages/{wp_id}/jobs", "/v1/n3/reviews",
            "/v1/n3/baselines/{baseline_id}/releases"} <= paths
    assert len([p for p in paths if p.startswith("/v1/n3/")]) >= 15


@pytest.mark.parametrize("name", ["n3_assessment", "n3_design_baseline"])
def test_live_records_validate_against_their_schema(flow, name):
    b = flow.ready()
    record = b if name == "n3_design_baseline" else \
        flow.work_package(flow.plan(b["baseline_id"], ["circuit_simulation"])["CIRCUIT_SIMULATION"])
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(record)
