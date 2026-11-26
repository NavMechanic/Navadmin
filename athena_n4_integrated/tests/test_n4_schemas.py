"""Published contracts: 21 N4 JSON Schema families, adapter formats, N8 records and the combined
N0_N1_N2_N3_N4_OpenAPI.json, all generated from the Pydantic models."""
from __future__ import annotations

import json

import jsonschema

from app.n4.models import N4_EXTRA_SCHEMAS, N4_SCHEMAS
from app.schema_export import expected_files
from n3_support import ROOT

SCHEMAS = ROOT / "schemas"


def test_twenty_one_n4_schema_families_are_published_and_valid():
    assert len(N4_SCHEMAS) == 21
    for name in list(N4_SCHEMAS) + list(N4_EXTRA_SCHEMAS) + ["n8_unit_genealogy", "n8_monitor", "n8_intake"]:
        schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    vr = json.loads((SCHEMAS / "payloads/VerificationReleasePayload.v1.schema.json").read_text())
    assert {"measurements", "traceability", "evidence_sha256", "approved_by"} <= set(vr["required"])


def test_committed_schemas_and_openapi_match_the_models():
    drift = [p for p, c in expected_files().items() if (SCHEMAS / p).read_text() != c]
    assert drift == [], f"run `python -m app.schema_export`: {drift}"


def test_openapi_covers_n4_and_n8():
    paths = set(json.loads((SCHEMAS / "N0_N1_N2_N3_N4_OpenAPI.json").read_text())["paths"])
    assert {"/v1/n4/intakes", "/v1/n4/plans/{plan_id}/evidence/scpi", "/v1/n4/plans/{plan_id}/evidence/scpi/live",
            "/v1/n4/plans/{plan_id}/evidence/lab", "/v1/n4/plans/{plan_id}/evidence/aoi", "/v1/n4/reviews",
            "/v1/n4/plans/{plan_id}/releases", "/v1/n8/intake", "/v1/n3/baselines", "/v1/n2/prds"} <= paths
    assert len([p for p in paths if p.startswith("/v1/n4/")]) >= 30


def test_live_records_validate_against_their_schemas(flow4):
    i, p, a = flow4.verified()
    for name, record in (("n4_design_intake", i), ("n4_test_plan", p), ("n4_verification_assessment", a)):
        jsonschema.Draft202012Validator(json.loads((SCHEMAS / f"{name}.schema.json").read_text())).validate(record)
