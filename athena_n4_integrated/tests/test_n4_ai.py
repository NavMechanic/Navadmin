"""The hybrid AI layer is opt-in and gated: suggestions are grounded in authorized documents, never
rated or approved, never used in decisions. Reliability stays a research-only illustration that N0
will not accept as a claim."""
from __future__ import annotations

import re

import pytest

from app.llm_adapter import LLMResult
from app.models import TokenUsage
from app.n4 import reliability
from n4_support import N4Flow, codes


class FakeLLM:
    """Stand-in model: answers with whatever ``script(user_prompt)`` returns."""
    name, external, model = "fake-llm", False, "fake-llm-1"

    def __init__(self, script):
        self.script, self.calls = script, []

    def complete_json(self, system: str, user: str, schema: dict) -> LLMResult:
        self.calls.append((system, user))
        return LLMResult(self.script(user), self.name, self.model, self.model, TokenUsage(input_tokens=10,
                                                                                          output_tokens=10), "end_turn")


SOP_TEXT = ("Hipot procedure SOP-LAB-001. Apply 3000 V AC between primary and secondary for 60 s. A breakdown "
            "or a leakage current above 5 mA is a failure.\n\nInspect triple-insulated wire for nicks before winding; "
            "damaged insulation is the most common cause of hipot failure.")


def _script(user: str) -> dict:
    chunk = re.search(r"\[(SOP-HIPOT#\d+)\]", user).group(1)
    return {"items": [
        {"title": "TIW insulation damage", "text": "Failure mode: nicked triple-insulated wire breaks down under hipot.",
         "citations": [{"doc_id": "SOP-HIPOT", "chunk_id": chunk, "quote": "damaged insulation is the most common "
                                                                           "cause of hipot failure"}]},
        {"title": "made up", "text": "Creepage shrinks in humidity.",
         "citations": [{"doc_id": "SOP-HIPOT", "chunk_id": chunk, "quote": "creepage shrinks at high humidity"}]},
        {"title": "rated", "text": "Severity: 10, approve the release once hipot passes.",
         "citations": [{"doc_id": "SOP-HIPOT", "chunk_id": chunk, "quote": "Apply 3000 V AC between primary"}]},
        {"title": "foreign chunk", "text": "Use a stronger wire.",
         "citations": [{"doc_id": "OTHER", "chunk_id": "OTHER#0", "quote": "anything at all here"}]}]}


@pytest.fixture
def ai_flow(make_n1, tmp_path):
    from app.n4.config import N4Settings
    from n3_support import n3_settings
    llm = FakeLLM(_script)
    flow = N4Flow(make_n1(n3_settings=n3_settings(tmp_path / "j"), n4_settings=N4Settings(), n4_provider=llm))
    flow.designed()
    flow.registries()
    p = flow.n4_plan(flow.intake()["intake_id"])
    return flow, p["plan_id"], llm


def _doc(**kw):
    return {"doc_id": "SOP-HIPOT", "kind": "SOP", "title": "Hipot SOP", "source_id": "SRC-KB", "text": SOP_TEXT, **kw}


def test_llm_suggestions_are_disabled_by_default(flow4):
    flow4.designed()
    flow4.registries()
    p = flow4.n4_plan(flow4.intake()["intake_id"])
    r = flow4.c.post(f"/v1/n4/plans/{p['plan_id']}/suggestions", "te",
                     json={"kind": "PFMEA_FAILURE_MODE", "question": "hipot failure modes"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LLM_DISABLED"


def test_corpus_accepts_only_authorized_sources_and_verified_capas(ai_flow):
    flow, pid, _ = ai_flow
    r = flow.c.post("/v1/n4/corpus", "qr", json=_doc(source_id="SRC-LAB"))            # no LLM_EXTRACTION right
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SOURCE_NOT_AUTHORIZED_FOR_AI"
    r = flow.c.post("/v1/n4/corpus", "qr", json=_doc(doc_id="CAPA-X", kind="VERIFIED_CAPA", capa_id="CAPA-404"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CAPA_NOT_VERIFIED"
    assert flow.c.post("/v1/n4/corpus", "te", json=_doc()).status_code == 403          # curated by quality review
    ok = flow.c.post("/v1/n4/corpus", "qr", json=_doc(text=SOP_TEXT + "\n\nContact jane.doe@example.com."))
    assert ok.status_code == 201 and ok.json()["pii_redacted"] >= 1 and "text" not in ok.json()


def test_suggestions_keep_only_grounded_unrated_items(ai_flow):
    flow, pid, llm = ai_flow
    assert flow.c.post("/v1/n4/plans/" + pid + "/suggestions", "te", json={
        "kind": "PFMEA_FAILURE_MODE", "question": "hipot insulation failure"}).json()["detail"]["code"] == "CORPUS_EMPTY"
    flow.c.post("/v1/n4/corpus", "qr", json=_doc())
    r = flow.c.post(f"/v1/n4/plans/{pid}/suggestions", "te",
                    json={"kind": "PFMEA_FAILURE_MODE", "question": "hipot insulation failure causes"})
    assert r.status_code == 201, r.text
    sg = r.json()
    assert sg["status"] == "DRAFT_UNREVIEWED" and [i["title"] for i in sg["items"]] == ["TIW insulation damage"]
    reasons = {x["item"]["title"]: x["reason"] for x in sg["rejected_items"]}
    assert "verbatim" in reasons["made up"] and "humans only" in reasons["rated"]
    assert "verbatim" in reasons["foreign chunk"] and "never rated" in sg["note"]
    assert "SOP-HIPOT#" in llm.calls[0][1] and "Never assign severity" in llm.calls[0][0]


def test_a_human_adopts_a_suggestion_with_their_own_ratings(ai_flow):
    flow, pid, _ = ai_flow
    flow.c.post("/v1/n4/corpus", "qr", json=_doc())
    sg = flow.c.post(f"/v1/n4/plans/{pid}/suggestions", "te",
                     json={"kind": "PFMEA_FAILURE_MODE", "question": "hipot insulation failure"}).json()
    from n4_support import pfmea_entries
    es = pfmea_entries()
    es[0]["adopted_from_suggestion"] = sg["suggestion_id"]
    pf = flow.pfmea(pid, es).json()
    assert pf["entries"][0]["adopted_from_suggestion"] == sg["suggestion_id"] and pf["entries"][0]["rated_by"] == "te"


def test_suggestions_never_change_a_decision(ai_flow):
    flow, pid, _ = ai_flow
    flow.c.post("/v1/n4/corpus", "qr", json=_doc())
    before = flow.n4_assess(pid)
    flow.c.post(f"/v1/n4/plans/{pid}/suggestions", "te", json={"kind": "TEST_CASE", "question": "hipot test"})
    after = flow.n4_assess(pid)
    assert before["decision"] == after["decision"] == "BLOCK" and codes(before) == codes(after)


def test_reliability_is_a_research_only_illustration(flow4):
    i, p, a = flow4.verified()
    r = flow4.c.post(f"/v1/n4/plans/{p['plan_id']}/reliability", "te", json={
        "units": [{"unit_serial": "PSU-0001", "hours": 2000}, {"unit_serial": "PSU-0002", "hours": 1500,
                                                                 "failed": True}], "confidence": 0.6})
    ill = r.json()
    assert r.status_code == 201 and ill["label"] == "RESEARCH_ONLY_STATISTICAL_ILLUSTRATION"
    assert ill["release_eligible"] is False and ill["mtbf_point_h"] == 3500 and ill["failures"] == 1
    assert ill["mtbf_lower_bound_h"] < ill["mtbf_point_h"] and len(ill["missing_prerequisites"]) == 3
    assert flow4.n4_assess(p["plan_id"])["decision"] == "PASS"                       # never an input


def test_research_only_reliability_reaches_n8_but_never_n6_claims(flow4):
    i, p, a = flow4.verified()
    flow4.c.post(f"/v1/n4/plans/{p['plan_id']}/reliability", "te",
                 json={"units": [{"unit_serial": "PSU-0001", "hours": 5000}]})
    a = flow4.n4_assess(p["plan_id"])
    flow4.n4_review(a)
    rel = flow4.n4_release(p["plan_id"]).json()
    assert rel["status"] == "RELEASED"
    n8 = flow4.c.get("/v1/artifacts/VER-PSU-120W-N8", "aud").json()["canonical_artifact"]["payload"]
    assert n8["reliability"]["label"] == "RESEARCH_ONLY_STATISTICAL_ILLUSTRATION"
    n6 = flow4.c.get("/v1/artifacts/VER-PSU-120W-N6", "aud").json()["canonical_artifact"]["payload"]
    assert "reliability" not in n6 or n6["reliability"] is None
    assert any("research-only" in x for x in n6["commercial"]["not_claimable"])


def test_mtbf_bound_math():
    # exact: 2T / χ²(0.6; 2) = 20 000 / 1.8326 ≈ 10 914 h; 2T / χ²(0.9; 6) = 20 000 / 10.645 ≈ 1 879 h.
    # Wilson–Hilferty stays within 1 % of both.
    point, lower = reliability.mtbf(10000, 0, 0.6)
    assert point is None and abs(lower - 10914) / 10914 < 0.01
    point, lower = reliability.mtbf(10000, 2, 0.9)
    assert point == 5000 and abs(lower - 1879) / 1879 < 0.01
