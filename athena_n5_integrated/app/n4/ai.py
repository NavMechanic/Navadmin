"""Hybrid AI layer. The deterministic policy engine (``engine.py``) is the only thing that decides.

| Layer | Status |
|---|---|
| Deterministic policy engine: limits, guard bands, calibration, traceability, hard blocks | ACTIVE |
| LLM + RAG drafts: PFMEA failure modes, test cases, RCA hypotheses | OPT_IN (``N4_LLM_PROVIDER``), gated |
| SLM extractor: test methods / instrument fields from reviewed documents | PLANNED (not implemented) |
| Vision DL, time-series ML, reliability models for release decisions | DEFERRED_GATED |

The RAG gate:

* the corpus holds only documents whose N0 source is APPROVED and permits ``LLM_EXTRACTION``
  (standards excerpts, SOPs) or that summarise a CAPA verified effective by quality review;
* text is PII-redacted before it can reach a model;
* every suggested item must cite a retrieved chunk with a verbatim quote, or it is dropped;
* items that set S / O / D ratings, action priority or approvals are dropped (humans only);
* suggestions are stored as ``DRAFT_UNREVIEWED`` and never enter a decision.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from jsonschema import Draft202012Validator

from ..llm_adapter import LLMError, LLMResult

STATUS = {
    "deterministic_policy_engine": "ACTIVE",
    "llm_rag_suggestions": "OPT_IN",
    "slm_extractor": "PLANNED",
    "vision_release_decisions": "DEFERRED_GATED",
    "time_series_ml": "DEFERRED_GATED",
    "reliability_models": "DEFERRED_GATED (research-only illustrations available)",
}
GATE_REQUIREMENTS = ["licensed training data", "double-labeled ground truth from independent human adjudication",
                     "independent holdout calibration", "N0 dataset policy gate approval"]
RATING = re.compile(r"\b(severity|occurrence|detection|rpn|action\s*priority|S|O|D)\s*[=:]\s*\d+|\bAP\s*[=:]\s*[HML]\b"
                    r"|\bapprov(e|ed|al)\s+(of\s+)?(the\s+|this\s+)?(release|unit|design|rating|severity|plan)", re.I)
SUGGESTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "maxItems": 10, "items": {
        "type": "object", "additionalProperties": False, "required": ["title", "text", "citations"],
        "properties": {"title": {"type": "string"}, "text": {"type": "string"},
                       "citations": {"type": "array", "minItems": 1, "items": {
                           "type": "object", "additionalProperties": False,
                           "required": ["doc_id", "chunk_id", "quote"],
                           "properties": {"doc_id": {"type": "string"}, "chunk_id": {"type": "string"},
                                          "quote": {"type": "string"}}}}}}}}}
SYSTEM = ("You draft {kind} suggestions for a hardware verification engineer. Use ONLY the numbered source chunks. "
          "Every item must cite at least one chunk with an exact, verbatim quote of at least 8 characters. Never "
          "assign severity, occurrence, detection, RPN or action-priority ratings, and never approve anything: "
          "humans rate and approve. If the sources do not support an item, leave it out. Answer in JSON only.")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]{2,}", s.lower())


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    text: str


def chunk(doc_id: str, text: str, size: int = 900) -> list[Chunk]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}".strip()
    if cur:
        out.append(cur)
    return [Chunk(doc_id, f"{doc_id}#{i}", t) for i, t in enumerate(out)]


def retrieve(chunks: list[Chunk], query: str, k: int) -> list[Chunk]:
    """BM25 over the authorized corpus (deterministic, no vector store)."""
    if not chunks:
        return []
    docs = [_tokens(c.text) for c in chunks]
    avg = sum(map(len, docs)) / len(docs)
    df = Counter(t for d in docs for t in set(d))
    q = _tokens(query)
    scores = []
    for c, d in zip(chunks, docs):
        tf = Counter(d)
        s = 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
        scores.append((s, c.chunk_id, c))
    return [c for s, _, c in sorted(scores, key=lambda x: (-x[0], x[1])) if s > 0][:k]


def validate(output: dict, retrieved: list[Chunk]) -> tuple[list[dict], list[dict]]:
    errors = sorted(Draft202012Validator(SUGGESTION_SCHEMA).iter_errors(output), key=str)
    if errors:
        raise LLMError(f"suggestion output failed schema validation: {errors[0].message}")
    by_id = {c.chunk_id: c for c in retrieved}
    kept, dropped = [], []
    for item in output["items"]:
        if RATING.search(item["title"] + " " + item["text"]):
            dropped.append({"item": item, "reason": "ratings, priorities and approvals are set by humans only"})
            continue
        good = [c for c in item["citations"] if c["chunk_id"] in by_id and by_id[c["chunk_id"]].doc_id == c["doc_id"]
                and len(c["quote"]) >= 8 and _norm(c["quote"]) in _norm(by_id[c["chunk_id"]].text)]
        if not good:
            dropped.append({"item": item, "reason": "no citation quotes a retrieved authorized chunk verbatim"})
            continue
        kept.append({**item, "citations": good})
    return kept, dropped


def suggest(provider, kind: str, question: str, retrieved: list[Chunk], max_items: int
            ) -> tuple[list[dict], list[dict], LLMResult]:
    sources = "\n\n".join(f"[{c.chunk_id}] (doc {c.doc_id})\n{c.text}" for c in retrieved)
    user = f"Question: {question}\nReturn at most {max_items} items.\n\nSources:\n{sources}"
    res = provider.complete_json(SYSTEM.format(kind=kind.replace("_", " ").lower()), user, SUGGESTION_SCHEMA)
    kept, dropped = validate(res.output, retrieved)
    return kept[:max_items], dropped, res


def make_provider(settings, override=None) -> Optional[object]:
    if override is not None:
        return override
    if settings.llm_provider == "anthropic":
        from ..llm_adapter import AnthropicProvider
        return AnthropicProvider(settings.llm_model)
    return None
