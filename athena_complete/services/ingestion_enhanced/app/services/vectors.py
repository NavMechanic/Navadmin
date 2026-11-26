"""Chunking and vector retrieval over ingested documents.

What the embedding is, and what it is honestly not
--------------------------------------------------
Retrieval here uses **hashed character n-gram vectors with TF-IDF weighting and
cosine similarity** — scikit-learn's ``TfidfVectorizer`` with a character
analyser. That is a lexical model, not a semantic one. It will find "thermal
resistance junction to case" from a query for "RthJC" because the strings
overlap; it will *not* connect "creepage" to "insulation coordination" the way a
sentence-transformer would.

That trade is deliberate for this build and it is stated rather than obscured:
character n-grams handle the notation soup in a datasheet (θJC, RthJC, Rth(j-c))
better than a general-purpose sentence embedding, they need no model download,
no GPU and no network, and they are deterministic — the same corpus produces the
same index every time, which matters when a retrieval result feeds an
engineering decision.

The seam for a real embedding model is ``Embedder``. Swapping in
sentence-transformers or an API embedder means implementing ``fit`` and
``encode``; nothing above this class knows which is in use, and ``/health``
reports which one is.

Chunking
--------
Chunks are page-aware with overlap. A datasheet parameter and its unit are
frequently split across a chunk boundary — "Thermal Resistance ... 1.8" in one
and "°C/W" in the next — and a chunk boundary that lands in the middle of a
parameter makes it unretrievable by either half. The overlap is what stops that.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

log = logging.getLogger(__name__)

try:  # pragma: no cover - depends on the environment
    from sklearn.feature_extraction.text import TfidfVectorizer
    _HAS_SKLEARN = True
except Exception:  # noqa: BLE001
    TfidfVectorizer = None  # type: ignore
    _HAS_SKLEARN = False

CHUNK_CHARS = 900
CHUNK_OVERLAP = 180


@dataclass(slots=True)
class Chunk:
    document_id: str
    chunk_index: int
    text: str
    page: int | None = None
    part_no: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.document_id}#{self.chunk_index}"


def chunk_pages(document_id: str, pages: Sequence[str], *,
                part_no: str | None = None,
                size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
                ) -> list[Chunk]:
    """Split page text into overlapping chunks, keeping the page number."""
    chunks: list[Chunk] = []
    index = 0
    for page_no, text in enumerate(pages, start=1):
        clean = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
        if not clean:
            continue
        if len(clean) <= size:
            chunks.append(Chunk(document_id, index, clean, page_no, part_no))
            index += 1
            continue
        start = 0
        while start < len(clean):
            end = min(len(clean), start + size)
            # Prefer to break on a line boundary so a table row stays whole.
            if end < len(clean):
                nl = clean.rfind("\n", start + size // 2, end)
                if nl > start:
                    end = nl
            piece = clean[start:end].strip()
            if piece:
                chunks.append(Chunk(document_id, index, piece, page_no, part_no))
                index += 1
            if end >= len(clean):
                break
            start = max(start + 1, end - overlap)
    return chunks


class Embedder:
    """The swap point for a real embedding model.

    ``backend`` is reported by /health so nobody mistakes a lexical index for a
    semantic one.
    """

    def __init__(self) -> None:
        self.backend = "tfidf-char-ngram" if _HAS_SKLEARN else "token-overlap"
        self._vectorizer: Any = None
        self._matrix: Any = None

    def fit(self, texts: Sequence[str]) -> None:
        if not _HAS_SKLEARN or not texts:
            return
        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5), min_df=1, max_features=60_000,
            sublinear_tf=True, lowercase=True)
        self._matrix = self._vectorizer.fit_transform(texts)

    def query(self, text: str, top_k: int) -> list[tuple[int, float]]:
        if self._vectorizer is None or self._matrix is None:
            return []
        vec = self._vectorizer.transform([text])
        # Both matrices are L2-normalised by TfidfVectorizer, so the dot product
        # is the cosine similarity directly.
        scores = (self._matrix @ vec.T).toarray().ravel()
        order = np.argsort(-scores)[:top_k]
        return [(int(i), float(scores[i])) for i in order if scores[i] > 0]


def _token_overlap(query: str, text: str) -> float:
    """Jaccard fallback when scikit-learn is absent. Weak, and labelled weak."""
    a = set(re.findall(r"[a-z0-9]+", query.lower()))
    b = set(re.findall(r"[a-z0-9]+", text.lower()))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class VectorIndex:
    """In-process index over ingested chunks.

    Rebuilt on write rather than updated incrementally: TF-IDF weights depend on
    the whole corpus, so adding a document without refitting leaves every earlier
    vector weighted against a corpus that no longer exists. At this corpus size
    refitting costs milliseconds; at a million chunks this moves to pgvector or
    Qdrant, and ``search`` is the interface that would not change.
    """

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._embedder = Embedder()
        self._dirty = False

    @property
    def backend(self) -> str:
        return self._embedder.backend

    def add(self, chunks: Sequence[Chunk]) -> int:
        self._chunks.extend(chunks)
        self._dirty = True
        return len(chunks)

    def remove_document(self, document_id: str) -> int:
        before = len(self._chunks)
        self._chunks = [c for c in self._chunks if c.document_id != document_id]
        self._dirty = True
        return before - len(self._chunks)

    def _refit(self) -> None:
        if self._dirty:
            self._embedder.fit([c.text for c in self._chunks])
            self._dirty = False

    def search(self, query: str, *, top_k: int = 5,
               part_no: str | None = None) -> list[dict[str, Any]]:
        if not self._chunks:
            return []
        self._refit()

        if _HAS_SKLEARN:
            hits = self._embedder.query(query, top_k * 4)
        else:
            scored = [(i, _token_overlap(query, c.text))
                      for i, c in enumerate(self._chunks)]
            hits = sorted((h for h in scored if h[1] > 0),
                          key=lambda h: -h[1])[:top_k * 4]

        out: list[dict[str, Any]] = []
        for idx, score in hits:
            chunk = self._chunks[idx]
            if part_no and chunk.part_no != part_no:
                continue
            out.append({
                "document_id": chunk.document_id, "chunk_index": chunk.chunk_index,
                "page": chunk.page, "part_no": chunk.part_no,
                "score": round(score, 5),
                "text": chunk.text[:600],
                "metadata": chunk.metadata})
            if len(out) >= top_k:
                break
        return out

    def stats(self) -> dict[str, Any]:
        return {
            "chunks": len(self._chunks),
            "documents": len({c.document_id for c in self._chunks}),
            "backend": self.backend,
            "sklearn": _HAS_SKLEARN,
            "note": (
                "Retrieval is lexical (character n-gram TF-IDF with cosine "
                "similarity), not semantic. It matches notation — RthJC against "
                "'thermal resistance junction to case' — and will not connect "
                "'creepage' to 'insulation coordination'. Character n-grams suit "
                "datasheet notation better than a general sentence embedding and "
                "are deterministic, which matters when a retrieval result feeds "
                "an engineering decision. Swap Embedder to change that."
                if _HAS_SKLEARN else
                "scikit-learn is absent, so retrieval is Jaccard token overlap. "
                "That is a keyword match, not a ranking — treat results as a "
                "filter, not a relevance order."),
        }


_index: VectorIndex | None = None


def get_index() -> VectorIndex:
    global _index
    if _index is None:
        _index = VectorIndex()
    return _index
