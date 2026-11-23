"""Tenant- and ACL-scoped retrieval for RAG (activity G: "no cross-tenant retrieval").

Isolation is enforced in the SQL query (tenant_id filter) before any scoring, then
per-document ACLs are applied. Scoring is a small BM25 over the permitted set; swap in
a vector index later without changing the isolation contract.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document

_TOKEN = re.compile(r"[a-z0-9]+")
INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (all |any )?(the )?(previous|prior|above|system)",
    r"you are now\b",
    r"(reveal|print|show|leak) (the |your )?(system prompt|api key|secret|password)",
    r"</?\s*(system|instructions?|document)\s*>",
    r"act as (an? )?(admin|developer|system)",
)]


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def injection_flags(text: str) -> list[str]:
    return [p.pattern for p in INJECTION_PATTERNS if p.search(text)]


def permitted(doc: Document, roles: frozenset[str]) -> bool:
    return not doc.acl_roles or "admin" in roles or bool(set(doc.acl_roles) & roles)


def search(db: Session, tenant_id: str, roles: frozenset[str], query: str, k: int = 4) -> list[dict]:
    docs = [d for d in db.scalars(select(Document).where(Document.tenant_id == tenant_id)).all() if permitted(d, roles)]
    q_terms = set(tokenize(query))
    if not docs or not q_terms:
        return []
    tokenized = {d.id: tokenize(d.title + " " + d.text) for d in docs}
    avg_len = sum(len(t) for t in tokenized.values()) / len(tokenized)
    df = Counter(term for toks in tokenized.values() for term in set(toks))
    n = len(docs)
    scored = []
    for d in docs:
        tf = Counter(tokenized[d.id])
        length = len(tokenized[d.id]) or 1
        score = 0.0
        for term in q_terms:
            if tf[term]:
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf[term] * 2.2 / (tf[term] + 1.2 * (0.25 + 0.75 * length / avg_len))
        if score > 0:
            scored.append((score, d))
    scored.sort(key=lambda x: (-x[0], x[1].id))
    return [{"id": d.id, "title": d.title, "text": d.text, "score": round(s, 4), "flags": injection_flags(d.text)}
            for s, d in scored[:k]]


def render_context(hits: list[dict]) -> str:
    """Wrap retrieved text as clearly delimited data; neutralise attempts to close the wrapper."""
    parts = []
    for h in hits:
        body = re.sub(r"</?\s*document[^>]*>", "[removed-tag]", h["text"], flags=re.I)
        parts.append(f'<document id="{h["id"]}" title="{h["title"]}">\n{body}\n</document>')
    return "\n".join(parts)
