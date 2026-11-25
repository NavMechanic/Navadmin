"""Optional market-report explainer. Disabled by default (``N1_EXPLAINER_PROVIDER=disabled``).

When enabled, it only *summarizes* a sealed ``MarketReport``. It gets the report's verified figures,
flags, missing information and pending attestations (never raw documents). It returns statements
that each cite ``evidence_id``s. Output is rejected deterministically if it:

* cites an evidence_id that is not in the report,
* contains a number that does not appear in the report (no LLM arithmetic, no invented figures),
* talks about a probability / chance / likelihood of winning (the score is a heuristic, not a probability).

Providers: ``template`` (deterministic text, local) and ``anthropic`` (Claude, external: every
contributing source must permit LLM_EXTRACTION).
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Optional

from jsonschema import Draft202012Validator

from ..llm_adapter import LLMError, LLMOutputInvalid, LLMResult
from ..models import TokenUsage
from .models import MarketReport, ReportExplanation

PROMPT_REVISION = "n1-report-explain/2026-11-25.1"
_STMT = {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["text", "evidence_ids"],
                                    "properties": {"text": {"type": "string"},
                                                   "evidence_ids": {"type": "array", "items": {"type": "string"}}}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary", "missing_information", "next_steps"],
          "properties": {"summary": _STMT, "missing_information": _STMT, "next_steps": _STMT}}
_VALIDATOR = Draft202012Validator(SCHEMA)
SYSTEM = ("You explain a market-intelligence report to a commercial team. Use only the facts provided. Every "
          "statement must cite at least one evidence_id from the facts. Copy numbers exactly as given; do not "
          "calculate, round or convert. The priority score is a heuristic ranking, never a probability of winning.")
_WIN_TALK = re.compile(r"\b(probabilit\w*|chance|likelihood|odds)\b[^.]{0,60}\bwin", re.I)
_NUMBER = re.compile(r"(?<![\w-])\d[\d,]*(?:\.\d+)?")


class ExplainerRejected(LLMError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def facts(report: MarketReport) -> dict[str, Any]:
    s = report.sam
    return {
        "report_id": report.report_id, "as_of": report.as_of.date().isoformat(), "base_currency": s.base_currency,
        "verified_real_sam": str(s.verified_real_sam), "verified_evidence_ids": s.verified_evidence_ids,
        "indicative_spend": str(s.indicative_spend), "indicative_evidence_ids": s.indicative_evidence_ids,
        "hypothetical_addressable_spend": str(s.hypothetical_addressable_spend),
        "hypothetical_evidence_ids": s.hypothetical_evidence_ids,
        "opportunities": [{"account": o.legal_name, "heuristic_priority_score": o.heuristic_priority_score,
                           "verified_spend": str(o.verified_spend_base), "risk_flags": o.risk_flags,
                           "evidence_ids": o.evidence_ids, "missing_information": o.missing_information}
                          for o in report.opportunities[:10]],
        "pending_attestations": report.pending_attestations,
        "exclusions": [{"evidence_id": x.evidence_id, "codes": x.codes} for x in report.exclusions],
    }


def _allowed_numbers(f: dict) -> set[str]:
    out: set[str] = set()

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            walk(str(v))
        elif isinstance(v, str):
            for m in _NUMBER.findall(v):
                out.add(m.replace(",", ""))
                try:
                    d = Decimal(m.replace(",", ""))
                    out.update({f"{d:,}".replace(",", ""), f"{d:.0f}", f"{d:.2f}", f"{d.normalize():f}"})
                except Exception:
                    pass
    walk(f)
    return out


def validate(output: dict, report: MarketReport) -> ReportExplanation:
    errors = list(_VALIDATOR.iter_errors(output))
    if errors:
        raise LLMOutputInvalid("; ".join(e.message for e in errors[:5]))
    known = {c.evidence_id for c in report.citations}
    allowed = _allowed_numbers(facts(report))
    for section in ("summary", "missing_information", "next_steps"):
        for st in output[section]:
            if not st["evidence_ids"]:
                raise ExplainerRejected("EXPLANATION_UNCITED", f"{section}: statement without evidence_ids")
            unknown = set(st["evidence_ids"]) - known
            if unknown:
                raise ExplainerRejected("EXPLANATION_UNKNOWN_EVIDENCE", f"{section}: cites {sorted(unknown)}")
            if _WIN_TALK.search(st["text"]):
                raise ExplainerRejected("EXPLANATION_WIN_PROBABILITY", "explanations may not describe a chance of "
                                                                       "winning")
            for num in _NUMBER.findall(st["text"]):
                if num.replace(",", "") not in allowed:
                    raise ExplainerRejected("EXPLANATION_UNVERIFIED_NUMBER", f"{section}: number {num} is not in "
                                                                             "the report")
    return ReportExplanation(report_id=report.report_id, provider="", model="", summary=output["summary"],
                             missing_information=output["missing_information"], next_steps=output["next_steps"])


class TemplateExplainer:
    name, external, model = "template", False, "athena-n1-template-explainer"

    def complete_json(self, system: str, user: str, schema: dict) -> LLMResult:
        f = json.loads(user)
        cur = f["base_currency"]
        any_ev = (f["verified_evidence_ids"] or f["indicative_evidence_ids"] or f["hypothetical_evidence_ids"]
                  or [e["evidence_id"] for e in f["exclusions"]])
        summary = []
        if f["verified_evidence_ids"]:
            summary.append({"text": f"Verified real SAM is {f['verified_real_sam']} {cur}.",
                            "evidence_ids": f["verified_evidence_ids"]})
        elif any_ev:
            summary.append({"text": f"No verified real SAM yet; verified_real_sam is {f['verified_real_sam']} {cur}.",
                            "evidence_ids": any_ev[:5]})
        if f["hypothetical_evidence_ids"]:
            summary.append({"text": f"Hypothetical addressable spend of {f['hypothetical_addressable_spend']} {cur} "
                                    "comes from synthetic or proxy data and is not SAM.",
                            "evidence_ids": f["hypothetical_evidence_ids"][:5]})
        for o in f["opportunities"][:3]:
            summary.append({"text": f"{o['account']} ranks with heuristic priority {o['heuristic_priority_score']} "
                                    f"(a prioritization score, not a probability).", "evidence_ids": o["evidence_ids"]})
        missing = [{"text": m, "evidence_ids": o["evidence_ids"]} for o in f["opportunities"][:5]
                   for m in o["missing_information"]]
        steps = [{"text": "Attest the pending verified-budget evidence before relying on it.",
                  "evidence_ids": f["pending_attestations"]}] if f["pending_attestations"] else []
        out = {"summary": summary, "missing_information": missing, "next_steps": steps}
        return LLMResult(out, self.name, self.model, "1.0.0", TokenUsage(input_tokens=len(user) // 4,
                                                                         output_tokens=len(json.dumps(out)) // 4),
                         "end_turn")


def make_explainer(settings, client=None):
    if settings.explainer_provider == "template":
        return TemplateExplainer()
    if settings.explainer_provider == "anthropic":
        from ..llm_adapter import AnthropicProvider
        return AnthropicProvider(settings.explainer_model, "low", client=client, max_tokens=8000)
    return None


def explain(provider, report: MarketReport) -> tuple[ReportExplanation, LLMResult]:
    f = facts(report)
    result = provider.complete_json(SYSTEM, json.dumps(f, ensure_ascii=False), SCHEMA)
    out = validate(result.output, report)
    return out.model_copy(update={"provider": result.provider, "model": result.model_version}), result


def model_number_set(report: MarketReport) -> set[str]:   # exposed for tests
    return _allowed_numbers(facts(report))


__all__ = ["ExplainerRejected", "PROMPT_REVISION", "TemplateExplainer", "explain", "facts", "make_explainer",
           "validate", "model_number_set", "Optional"]
