"""SLM extraction worker: disabled by default, refused until it passes the held-out gold set,
grounded in verbatim evidence, rights-gated for external models, PII-redacted first."""
from __future__ import annotations

from app.llm_adapter import LLMResult
from app.models import TokenUsage
from app.n1.config import N1Settings
from app.n1.slm_extractor import RuleBasedSlm, ground
from conftest import NOW, iso

TEXT = ("Contracting authority: Example Water Board\nCountry: NL\nEstimated value: EUR 640,000\n"
        "Quantity: 150 units\nDeadline: 2026-12-30\nPayment terms: 45 days net\n"
        "- Battery charger with 110 V DC output\nContact: tenders@example-water.test\n")


class SpySlm:
    def __init__(self, external=False, lazy=False):
        self.name, self.model = ("spy-ext" if external else "spy-local"), "spy-slm-1"
        self.external, self.lazy, self.calls = external, lazy, []

    def complete_json(self, system, user, schema):
        self.calls.append(user)
        out = RuleBasedSlm().complete_json(system, user, schema).output
        if self.lazy:
            for k in ("buyer_name", "buyer_country", "deadline", "quantity", "payment_terms"):
                out[k] = {"value": None, "evidence_text": None}
            out["amount"] = {"value": None, "currency": None, "evidence_text": None}
            out["service_categories"] = []
        return LLMResult(out, self.name, self.model, self.model, TokenUsage(input_tokens=50, output_tokens=20), "end_turn")


def _extract(n1, **kw):
    return n1.post("/v1/n1/slm/extract", "ana", json={"tenant_id": "acme", "source_id": "SRC-TED",
                                                      "evidence_id": "SLM-1", "text": TEXT, "published_at": iso(NOW),
                                                      **kw})


def test_slm_is_disabled_by_default(n1_src):
    assert _extract(n1_src).json()["detail"]["code"] == "SLM_DISABLED"
    assert n1_src.post("/v1/n1/slm/evaluate", "apr").status_code == 503


def test_extraction_is_refused_until_the_gold_set_passes(make_n1):
    n1 = make_n1(n1_settings=N1Settings(slm_provider="rules"))
    n1.source()
    r = _extract(n1)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SLM_NOT_QUALIFIED"
    assert n1.post("/v1/n1/slm/evaluate", "ana").status_code == 403                   # approvers qualify models
    ev = n1.post("/v1/n1/slm/evaluate", "apr").json()
    assert ev["passed"] is True and ev["n_examples"] == 10 and ev["metrics"]["hallucination_rate"] == 0.0
    assert n1.get("/v1/n1/status", "ana").json()["slm"]["qualified"] is True
    body = _extract(n1).json()
    o = body["observation"]
    assert o["extraction_method"] == "SLM" and o["extraction_model_run_id"] == body["model_run"]["run_id"]
    assert o["buyer_name"] == "Example Water Board" and o["amount"] == {"amount": "640000.0", "currency": "EUR"}
    assert o["quantity"] == 150 and o["deadline_at"].startswith("2026-12-30") and o["service_category"] == "power_electronics"
    assert all(s["text"] in TEXT or "<EMAIL_ADDRESS>" in s["text"] for s in o["evidence_snippets"])
    assert "tenders@example-water.test" not in o["description"]                     # stored text is redacted
    assert body["model_run"]["prompt_revision"].startswith("n1-tender-extract/")


def test_a_failing_model_stays_disabled(make_n1):
    lazy = SpySlm(lazy=True)
    n1 = make_n1(slm_provider=lazy)
    n1.source()
    ev = n1.post("/v1/n1/slm/evaluate", "apr").json()
    assert ev["passed"] is False and ev["metrics"]["buyer_name_accuracy"] < 0.9 and ev["failures"]
    assert _extract(n1).json()["detail"]["code"] == "SLM_NOT_QUALIFIED"


def test_external_slm_needs_llm_extraction_rights_and_sees_only_redacted_text(make_n1):
    spy = SpySlm(external=True)
    n1 = make_n1(slm_provider=spy)
    n1.source(permitted_use=("MARKET_INTELLIGENCE",))
    n1.source("SRC-LLM", permitted_use=("MARKET_INTELLIGENCE", "LLM_EXTRACTION"))
    n1.post("/v1/n1/slm/evaluate", "apr")
    spy.calls.clear()
    r = _extract(n1)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LLM_TRANSMISSION_UNAPPROVED" and spy.calls == []
    assert _extract(n1, source_id="SRC-LLM").status_code == 200
    assert "tenders@example-water.test" not in spy.calls[0] and "<EMAIL_ADDRESS>" in spy.calls[0]


def test_values_without_verbatim_evidence_are_dropped():
    out = RuleBasedSlm().complete_json("", f"<document>\n{TEXT}\n</document>", {}).output
    out["amount"] = {"value": 9_999_999, "currency": "EUR", "evidence_text": "Estimated value: EUR 9,999,999"}
    out["buyer_country"] = {"value": "Netherlands", "evidence_text": "Country: NL"}
    g = ground(out, TEXT)
    assert "amount" not in g.values and "buyer_country" not in g.values
    assert any("evidence quote not found" in w for w in g.warnings) and any("ISO alpha-2" in w for w in g.warnings)
