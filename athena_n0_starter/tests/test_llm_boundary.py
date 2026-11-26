"""LLM boundary: disabled by default, rights- and tenant-gated before any provider call, PII
redacted first, schema-constrained output (nulls + missing_fields + evidence), always revalidated
by the deterministic rules, never PASS on its own."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import ModelRunRow
from app.llm_adapter import (EXTRACTION_SCHEMA, RFQ_PARAMETERS, AnthropicProvider, DeterministicRFQProvider,
                             LLMRefusalError, LLMResult, estimate_cost_usd)
from app.models import TokenUsage
from app.rules import sha256_hex
from conftest import EXAMPLES, RFQ_SOURCE

DOC = (EXAMPLES / "rfq_email.txt").read_bytes()


class SpyProvider:
    """Records exactly what would have left the process."""
    name, model = "spy", "spy-model"

    def __init__(self, external: bool = True, output: dict | None = None, exc: Exception | None = None) -> None:
        self.external, self.output, self.exc, self.calls = external, output, exc, []

    def complete_json(self, system: str, user: str, schema: dict) -> LLMResult:
        self.calls.append(user)
        if self.exc is not None:
            raise self.exc
        out = self.output if self.output is not None else \
            DeterministicRFQProvider().complete_json(system, user, schema).output
        return LLMResult(out, "spy", "spy-model", "spy-model-2026", TokenUsage(input_tokens=900, output_tokens=150),
                         "end_turn")


def _output(**values) -> dict:
    """A schema-valid model output; ``values`` maps parameter → (value, unit, evidence)."""
    params = [{"parameter": p, "value": values[p][0], "unit": values[p][1], "evidence_text": values[p][2]}
              if p in values else {"parameter": p, "value": None, "unit": None, "evidence_text": None}
              for p in RFQ_PARAMETERS]
    return {"customer_name": "Acme Industrial Controls", "product_category": "power_supply", "quantity": 2500,
            "parameters": params, "missing_fields": [p for p in RFQ_PARAMETERS if p not in values]}


def _setup(n0, permitted=("RFQ_ANALYSIS", "LLM_EXTRACTION"), approve=True) -> str:
    n0.source(permitted_use=permitted, approve=approve)
    r = n0.upload(DOC, "rfq_email.txt")
    assert r.status_code == 201, r.text
    return r.json()["blob_id"]


def _extract(n0, blob_id: str, who: str = "alice", tenant: str = "acme"):
    return n0.post("/v1/extraction/run", who, json={"tenant_id": tenant, "project_id": "PRJ-PSU-24V-120W",
                                                    "source_id": RFQ_SOURCE, "blob_id": blob_id})


def _runs(n0) -> list[dict]:
    with n0.app.state.db.session() as s:
        return [r.record for r in s.scalars(select(ModelRunRow))]


# ------------------------------------------------------------------------------------------------
# baseline
# ------------------------------------------------------------------------------------------------
def test_llm_is_disabled_by_default(make_n0):
    n0 = make_n0(llm_provider="disabled")
    blob = _setup(n0)
    r = _extract(n0, blob)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "LLM_DISABLED"
    assert n0.c.get("/health").json()["components"]["llm"]["enabled"] is False
    assert _runs(n0) == []


def test_unapproved_source_never_reaches_the_model(make_n0):
    spy = SpyProvider()
    n0 = make_n0(provider=spy)
    blob = _setup(n0, approve=False)                       # quarantine upload is allowed while PENDING
    r = _extract(n0, blob)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SOURCE_NOT_APPROVED"
    assert spy.calls == [] and _runs(n0) == []


def test_extraction_returns_nulls_missing_fields_and_evidence(n0):
    blob = _setup(n0)
    r = _extract(n0, blob)
    assert r.status_code == 200, r.text
    body = r.json()
    draft, run = body["draft"], body["model_run"]
    reqs = {q["parameter"]: q for q in draft["requirements"]}
    stated = {"input_voltage_v": 48, "output_voltage_v": 24, "output_current_a": 5, "operating_temp_min_c": -40,
              "operating_temp_max_c": 85}
    text = DOC.decode()
    for p, v in stated.items():
        q = reqs[p]
        assert q["value"] == v and q["value_origin"] == "MODEL_EXTRACTED"
        ev = q["evidence_snippets"][0]
        assert ev["source_ref"] == blob and ev["text"] in text
        assert text[ev["char_start"]:ev["char_end"]] == ev["text"]
    # never invented: unstated values are absent and listed as missing, not guessed or computed
    for p in ("output_power_w", "efficiency_pct", "switching_frequency_hz", "isolation_voltage_v"):
        assert p not in reqs and p in draft["missing_fields"]
    # N0's own arithmetic is DERIVED, with formula and inputs — never MODEL_EXTRACTED
    power = reqs["nominal_output_power_w"]
    assert power["value"] == 120 and power["value_origin"] == "DERIVED"
    assert power["derivation"]["inputs"] == ["output_voltage_v:EXT-002", "output_current_a:EXT-003"]
    assert draft["customer_name"] == "Acme Industrial Controls" and draft["quantity"] == 2500
    assert body["decision_preview"] == "REVIEW"
    assert {v["code"] for v in body["violated_rules"]} == {"HUMAN_CONFIRMATION_REQUIRED"}
    assert run["prompt_revision"].startswith("rfq-extract/") and len(run["input_hash"]) == 64
    assert run["status"] == "SUCCEEDED" and run["token_usage"]["input_tokens"] > 0
    assert {f["entity_type"] for f in body["pii_findings"]} >= {"EMAIL_ADDRESS", "PHONE_NUMBER"}
    assert [x["run_id"] for x in _runs(n0)] == [run["run_id"]]


# ------------------------------------------------------------------------------------------------
# transmission boundary
# ------------------------------------------------------------------------------------------------
def test_external_model_requires_llm_extraction_rights(make_n0):
    spy = SpyProvider(external=True)
    n0 = make_n0(provider=spy)
    blob = _setup(n0, permitted=("RFQ_ANALYSIS",))
    r = _extract(n0, blob)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LLM_TRANSMISSION_UNAPPROVED"
    assert spy.calls == []


def test_local_extractor_needs_no_transmission_grant(make_n0):
    spy = SpyProvider(external=False)
    n0 = make_n0(provider=spy)
    blob = _setup(n0, permitted=("RFQ_ANALYSIS",))
    assert _extract(n0, blob).status_code == 200 and len(spy.calls) == 1


def test_pii_is_redacted_before_the_model_sees_the_document(make_n0):
    spy = SpyProvider(external=True)
    n0 = make_n0(provider=spy)
    _ = _extract(n0, _setup(n0))
    sent = spy.calls[0]
    assert "procurement@acme-controls.example" not in sent and "1234 5678" not in sent
    assert "<EMAIL_ADDRESS>" in sent and "Output voltage: 24 V" in sent
    assert "untrusted data" not in sent                     # the system prompt travels separately from the document


def test_cross_tenant_extraction_is_refused(n0):
    blob = _setup(n0)
    r = _extract(n0, blob, who="gp", tenant="acme")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "CROSS_TENANT_REFERENCE"
    n0.source(tenant="globex")
    r = _extract(n0, blob, who="gp", tenant="globex")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "BLOB_NOT_FOUND"


# ------------------------------------------------------------------------------------------------
# revalidation of model output
# ------------------------------------------------------------------------------------------------
def test_schema_invalid_model_output_is_rejected_and_logged(make_n0):
    n0 = make_n0(provider=SpyProvider(output={"customer_name": "x", "answer": 42}))
    r = _extract(n0, _setup(n0))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "MODEL_OUTPUT_SCHEMA_INVALID"
    runs = _runs(n0)
    assert len(runs) == 1 and runs[0]["status"] == "FAILED" and runs[0]["run_id"] == r.json()["detail"]["model_run_id"]


def test_hallucinated_evidence_is_caught(make_n0):
    out = _output(output_voltage_v=(48, "V", "Output voltage: 48 V"), output_current_a=(5, "A", "Output current: 5 A"))
    n0 = make_n0(provider=SpyProvider(output=out))
    body = _extract(n0, _setup(n0)).json()
    assert "EVIDENCE_NOT_FOUND" in {v["code"] for v in body["violated_rules"]}
    assert body["decision_preview"] == "REWORK"


def test_value_without_evidence_is_rework(make_n0):
    out = _output(output_voltage_v=(24, "V", None))
    n0 = make_n0(provider=SpyProvider(output=out))
    body = _extract(n0, _setup(n0)).json()
    assert {"EVIDENCE_MISSING", "MISSING_CRITICAL_FIELD"} <= {v["code"] for v in body["violated_rules"]}
    assert body["decision_preview"] == "REWORK"


def test_model_output_physics_is_rechecked(make_n0):
    out = _output(output_voltage_v=(24, "V", "Output voltage: 24 V"), output_current_a=(5, "A", "Output current: 5 A"),
                  output_power_w=(150, "W", "Output voltage: 24 V"))
    n0 = make_n0(provider=SpyProvider(output=out))
    body = _extract(n0, _setup(n0)).json()
    assert "POWER_BALANCE_MISMATCH" in {v["code"] for v in body["violated_rules"]}


# ------------------------------------------------------------------------------------------------
# Claude provider contract (no network: the SDK client is faked)
# ------------------------------------------------------------------------------------------------
class _FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _fake_client(stop_reason="end_turn", payload=None, category=None):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    content = [SimpleNamespace(type="text", text=json.dumps(payload))] if payload is not None else []
    resp = SimpleNamespace(stop_reason=stop_reason, content=content, usage=usage, model="claude-opus-5-5",
                           stop_details=SimpleNamespace(category=category) if category else None)
    return SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages(resp)))


def test_anthropic_provider_requests_schema_constrained_output():
    client = _fake_client(payload=_output(output_voltage_v=(24, "V", "Output voltage: 24 V")))
    result = AnthropicProvider(client=client).complete_json("sys", "<document>\nx\n</document>", EXTRACTION_SCHEMA)
    kw = client.beta.messages.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": EXTRACTION_SCHEMA}
    assert kw["output_config"]["effort"] == "medium"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert kw["system"] == "sys" and kw["messages"][0]["role"] == "user"
    assert result.model_version == "claude-opus-5-5" and result.usage.output_tokens == 200
    assert estimate_cost_usd("claude-opus-5-5", result.usage) == pytest.approx(0.008)


def test_anthropic_refusal_is_surfaced_not_parsed():
    with pytest.raises(LLMRefusalError):
        AnthropicProvider(client=_fake_client(stop_reason="refusal", category="cyber")).complete_json(
            "s", "u", EXTRACTION_SCHEMA)


def test_refused_run_is_logged_and_returns_422(make_n0):
    n0 = make_n0(provider=AnthropicProvider(client=_fake_client(stop_reason="refusal", category="cyber")))
    r = _extract(n0, _setup(n0))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LLM_REFUSED"
    assert [x["status"] for x in _runs(n0)] == ["REFUSED"]


def test_input_hash_binds_prompt_revision_and_redacted_text(n0):
    from app.llm_adapter import PROMPT_REVISION
    body = _extract(n0, _setup(n0)).json()
    redacted, _ = n0.app.state.presidio.redact(DOC.decode())
    assert body["model_run"]["input_hash"] == sha256_hex(f"{PROMPT_REVISION}\n{redacted}".encode())
