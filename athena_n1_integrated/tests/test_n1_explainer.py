"""Opt-in report explainer: cites evidence, repeats only numbers from the report, never claims a
probability of winning, and needs LLM_EXTRACTION rights for external models."""
from __future__ import annotations

import pytest

from app.n1.llm_adapter import ExplainerRejected, TemplateExplainer, validate
from app.n1.models import MarketReport
from conftest import award, tender


@pytest.fixture
def explained(make_n1):
    n1 = make_n1(explainer_provider=TemplateExplainer())
    n1.source()
    n1.submit_ok(tender(), award())
    n1.attest("TED-2026-000202")
    return n1, n1.report()


def test_template_explanation_is_cited_and_number_safe(explained):
    n1, rep = explained
    r = n1.post(f"/v1/n1/reports/{rep['report_id']}/explain", "ana")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] and all(s["evidence_ids"] for s in body["summary"])
    assert any("432000.00" in s["text"] for s in body["summary"]) and body["model_run_id"]


@pytest.mark.parametrize("statement, code", [
    ({"text": "Verified SAM is 999999.00 USD.", "evidence_ids": ["TED-2026-000202"]}, "EXPLANATION_UNVERIFIED_NUMBER"),
    ({"text": "Strong buyer.", "evidence_ids": ["NOT-IN-REPORT"]}, "EXPLANATION_UNKNOWN_EVIDENCE"),
    ({"text": "There is a high chance we will win this tender.", "evidence_ids": ["TED-2026-000202"]},
     "EXPLANATION_WIN_PROBABILITY"),
    ({"text": "Uncited claim.", "evidence_ids": []}, "EXPLANATION_UNCITED"),
])
def test_unsafe_model_statements_are_rejected(explained, statement, code):
    _, rep = explained
    with pytest.raises(ExplainerRejected) as exc:
        validate({"summary": [statement], "missing_information": [], "next_steps": []}, MarketReport.model_validate(rep))
    assert exc.value.code == code


def test_external_explainer_needs_llm_rights(make_n1):
    class External(TemplateExplainer):
        name, external = "ext", True
    n1 = make_n1(explainer_provider=External())
    n1.source()
    n1.submit_ok(award())
    rep = n1.report()
    r = n1.post(f"/v1/n1/reports/{rep['report_id']}/explain", "ana")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LLM_TRANSMISSION_UNAPPROVED"
