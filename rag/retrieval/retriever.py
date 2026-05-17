"""Hybrid retrieval combining dense vector search with BM25."""

from typing import Optional

import numpy as np
from rank_bm25 import BM25Okapi

from rag.chunking.strategies import TextChunk
from rag.config import RetrievalConfig
from rag.embeddings.embedder import Embedder
from rag.vectorstore.store import VectorStore


class Retriever:
    """Performs hybrid retrieval: dense vector search + BM25 on candidates."""

    def __init__(
        self,
        vector_store: VectorStore,
        embedder: Embedder,
        config: RetrievalConfig,
    ) -> None:
        self.vector_store = vector_store
        self.embedder = embedder
        self.config = config

        # BM25 index built over ingested chunks
        self._bm25_corpus: list[list[str]] = []
        self._bm25_ids: list[str] = []
        self._bm25: Optional[BM25Okapi] = None

    # ------------------------------------------------------------------
    # BM25 index management
    # ------------------------------------------------------------------

    def index_for_bm25(self, chunks: list[TextChunk]) -> None:
        """Build (or extend) the in-memory BM25 index from ingested chunks."""
        for chunk in chunks:
            chunk_id = f"chunk_{chunk.chunk_index}"
            tokenized = chunk.text.lower().split()
            self._bm25_corpus.append(tokenized)
            self._bm25_ids.append(chunk_id)

        if self._bm25_corpus:
            self._bm25 = BM25Okapi(self._bm25_corpus)

    def rebuild_bm25_from_store(self) -> None:
        """Rebuild the BM25 index by scrolling all documents from the vector store.

        Use this after switching to a new vector store backend (e.g. Qdrant)
        to restore hybrid retrieval without re-ingesting documents.
        """
        self._bm25_corpus = []
        self._bm25_ids = []
        self._bm25 = None

        count = self.vector_store.get_collection_count()
        if count == 0:
            return

        # Scroll all points from the Qdrant collection
        try:
            scroll_result = self.vector_store._client.scroll(
                collection_name=self.vector_store.config.collection_name,
                limit=count,
                with_payload=True,
                with_vectors=False,
            )
            points = scroll_result[0]
        except Exception:
            # Fall back gracefully if the store doesn't support scroll
            return

        for point in points:
            payload = point.payload or {}
            text = payload.get("text", "")
            chunk_index = payload.get("chunk_index", str(point.id))
            chunk_id = f"chunk_{chunk_index}"
            if text:
                tokenized = text.lower().split()
                self._bm25_corpus.append(tokenized)
                self._bm25_ids.append(chunk_id)

        if self._bm25_corpus:
            self._bm25 = BM25Okapi(self._bm25_corpus)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def retrieve(self, query: str) -> list[dict]:
        """Retrieve the most relevant chunks for a query.

        Performs dense retrieval and, when hybrid mode is enabled, combines
        with BM25 scores on the returned candidates.
        """
        query_vector = self.embedder.embed_query(query)
        candidates = self.vector_store.similarity_search(
            query_vector, top_k=self.config.top_k
        )

        if not candidates:
            return []

        if not self.config.use_hybrid or self._bm25 is None:
            return candidates

        # Hybrid: merge dense scores with BM25 scores
        bm25_scores = self._bm25_search(query, candidates)

        vector_weight = 1.0 - self.config.bm25_weight
        bm25_weight = self.config.bm25_weight

        # Normalize BM25 scores to [0, 1]
        bm25_values = list(bm25_scores.values())
        max_bm25 = max(bm25_values) if bm25_values else 1.0
        if max_bm25 == 0.0:
            max_bm25 = 1.0

        merged: list[dict] = []
        for candidate in candidates:
            cid = candidate["id"]
            raw_bm25 = bm25_scores.get(cid, 0.0)
            normalized_bm25 = raw_bm25 / max_bm25
            combined_score = (
                vector_weight * candidate["score"] + bm25_weight * normalized_bm25
            )
            merged.append({**candidate, "score": combined_score})

        merged.sort(key=lambda x: x["score"], reverse=True)
        return merged

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _bm25_search(
        self, query: str, candidates: list[dict]
    ) -> dict[str, float]:
        """Score candidates with BM25 and return id -> score mapping."""
        if self._bm25 is None:
            return {}

        candidate_ids = {c["id"] for c in candidates}
        query_tokens = query.lower().split()

        # Get scores for all indexed documents
        all_scores = self._bm25.get_scores(query_tokens)

        id_to_score: dict[str, float] = {}
        for idx, doc_id in enumerate(self._bm25_ids):
            if doc_id in candidate_ids:
                id_to_score[doc_id] = float(all_scores[idx])

        return id_to_score
