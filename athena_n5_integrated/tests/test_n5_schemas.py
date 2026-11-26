"""Published contracts: 19 N5 JSON Schema families, adapter / record schemas, N6 / N7 records, the
SourcingHandoffPayload.v1 and the combined N0_N1_N2_N3_N4_N5_OpenAPI.json, generated from the models."""
from __future__ import annotations

import json

import jsonschema

from app.n5.models import N5_EXTRA_SCHEMAS, N5_SCHEMAS
from app.schema_export import expected_files
from n3_support import ROOT

SCHEMAS = ROOT / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def test_nineteen_n5_schema_families_are_published_and_valid():
    assert len(N5_SCHEMAS) == 19
    for name in list(N5_SCHEMAS) + list(N5_EXTRA_SCHEMAS) + ["n6_bid_input", "n7_cost_record"]:
        schema = _schema(name)
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    sh = json.loads((SCHEMAS / "payloads/SourcingHandoffPayload.v1.schema.json").read_text())
    assert {"lines", "indicative_total", "indicative_scope", "excluded_cost_categories", "quote_source_ids",
            "report_sha256", "quote_hashes", "approved_by", "bom_ref"} <= set(sh["required"])


def test_money_is_never_a_float_in_the_contracts():
    sl = json.loads((SCHEMAS / "payloads/SourcingHandoffPayload.v1.schema.json").read_text())["$defs"]["SourcedLine"]
    for field in ("unit_price", "extended_cost", "extended_cost_base"):
        types = {t.get("type") for t in sl["properties"][field].get("anyOf", [sl["properties"][field]])}
        assert "string" in types, field


def test_committed_schemas_and_openapi_match_the_models():
    drift = [p for p, c in expected_files().items() if (SCHEMAS / p).read_text() != c]
    assert drift == [], f"run `python -m app.schema_export`: {drift}"


def test_openapi_covers_n5_n6_n7():
    paths = set(json.loads((SCHEMAS / "N0_N1_N2_N3_N4_N5_OpenAPI.json").read_text())["paths"])
    assert {"/v1/n5/bom-intakes", "/v1/n5/suppliers", "/v1/n5/suppliers/{supplier_id}/review", "/v1/n5/quotes",
            "/v1/n5/quotes/ems", "/v1/n5/bom-intakes/{intake_id}/lookups", "/v1/n5/reports", "/v1/n5/reviews",
            "/v1/n5/reports/{report_id}/handoffs", "/v1/n5/eco-proposals", "/v1/n6/intake", "/v1/n7/intake",
            "/v1/n4/intakes", "/v1/n3/baselines", "/v1/n2/prds"} <= paths
    assert len([p for p in paths if p.startswith("/v1/n5/")]) >= 16


def test_live_records_validate_against_their_schemas(flow5):
    intake, rep, h = flow5.handed_off()
    quote = flow5.c.get(f"/v1/n5/quotes/{rep['quote_ids'][0]}", "pan").json()
    for name, record in (("n5_bom_intake", intake), ("n5_sourcing_report", rep), ("n5_handoff", h),
                         ("n5_quote", quote)):
        jsonschema.Draft202012Validator(_schema(name)).validate(record)
