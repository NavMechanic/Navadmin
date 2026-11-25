"""Baseline — N0's LLM boundary inside the integrated app."""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.llm_adapter import EXTRACTION_SCHEMA, AnthropicProvider, DeterministicRFQProvider, LLMResult
from app.models import TokenUsage

DOC = (b"Customer: Example Industrial Controls\nProduct category: power supply\nQuantity: 500\n"
       b"Input voltage: 48 V\nOutput voltage: 24 V\nOutput current: 5 A\nOperating temperature: -40 to +85 C\n")


class Spy:
    name, model = "spy", "spy-model"

    def __init__(self, external=True):
        self.external, self.calls = external, []

    def complete_json(self, system, user, schema):
        self.calls.append(user)
        out = DeterministicRFQProvider().complete_json(system, user, schema).output
        return LLMResult(out, "spy", "spy", "spy-1", TokenUsage(input_tokens=10, output_tokens=5), "end_turn")


def _upload(n1):
    r = n1.post("/v1/artifacts/upload", "ana", data={"tenant_id": "acme", "source_id": "SRC-TED"},
                files={"file": ("rfq.txt", DOC, "text/plain")})
    assert r.status_code == 201, r.text
    return r.json()["blob_id"]


def _extract(n1, blob):
    return n1.post("/v1/extraction/run", "ana", json={"tenant_id": "acme", "project_id": "PRJ-1",
                                                      "source_id": "SRC-TED", "blob_id": blob})


def test_n0_llm_is_disabled_by_default(n1_src):
    r = _extract(n1_src, _upload(n1_src))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "LLM_DISABLED"


def test_unapproved_source_never_reaches_the_model(make_n1):
    spy = Spy()
    n1 = make_n1(provider=spy)
    n1.source(approve=False)
    r = _extract(n1, _upload(n1))
    assert r.status_code == 403 and spy.calls == []


def test_extraction_nulls_missing_fields_and_evidence(make_n1):
    n1 = make_n1(llm_provider="deterministic")
    n1.source(permitted_use=("RFQ_ANALYSIS", "LLM_EXTRACTION"))
    body = _extract(n1, _upload(n1)).json()
    assert body["decision_preview"] == "REVIEW" and "efficiency_pct" in body["missing_fields"]
    reqs = {q["parameter"]: q for q in body["draft"]["requirements"]}
    assert reqs["output_voltage_v"]["value_origin"] == "MODEL_EXTRACTED" and reqs["output_voltage_v"]["evidence_snippets"]
    assert reqs["nominal_output_power_w"]["value_origin"] == "DERIVED"


def test_external_model_needs_llm_extraction_rights(make_n1):
    spy = Spy(external=True)
    n1 = make_n1(provider=spy)
    n1.source(permitted_use=("RFQ_ANALYSIS",))
    r = _extract(n1, _upload(n1))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LLM_TRANSMISSION_UNAPPROVED" and spy.calls == []


def test_server_side_fallback_only_for_models_that_offer_it():
    payload = {"customer_name": None, "product_category": None, "quantity": None, "parameters": [], "missing_fields": []}
    resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                           usage=SimpleNamespace(input_tokens=5, output_tokens=5, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0), model="m", stop_details=None)
    calls = {}
    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **k: calls.setdefault("beta", k) and resp)),
                           messages=SimpleNamespace(create=lambda **k: calls.setdefault("plain", k) and resp))
    AnthropicProvider("claude-opus-5-5", client=fake).complete_json("s", "u", EXTRACTION_SCHEMA)
    AnthropicProvider("claude-haiku-5-5", client=fake).complete_json("s", "u", EXTRACTION_SCHEMA)
    assert calls["beta"]["fallbacks"] == "default" and calls["beta"]["model"] == "claude-opus-5-5"
    assert "fallbacks" not in calls["plain"] and calls["plain"]["model"] == "claude-haiku-5-5"
    assert calls["plain"]["output_config"]["format"]["type"] == "json_schema"
