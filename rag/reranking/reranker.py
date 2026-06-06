"""Cross-encoder reranking to refine retrieval results."""

from typing import Optional

from rag.config import RerankerConfig


class Reranker:
    """Reranks retrieval candidates using a cross-encoder model."""

    def __init__(self, config: RerankerConfig) -> None:
        self.config = config
        self._cross_encoder = None  # lazy-loaded

    def _ensure_model(self) -> None:
        """Lazily load the cross-encoder model."""
        if self._cross_encoder is None:
            from sentence_transformers import CrossEncoder

            self._cross_encoder = CrossEncoder(self.config.model_name)

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        """Rerank candidates and return the top_n most relevant.

        Args:
            query: The user query string.
            candidates: List of dicts containing at least a ``text`` key.

        Returns:
            List of top_n candidates, sorted by reranking score (descending).
            Each dict gets an added ``rerank_score`` key.
        """
        if not self.config.enabled:
            return candidates[: self.config.top_n]

        if not candidates:
            return []

        self._ensure_model()

        pairs = [(query, c["text"]) for c in candidates]
        scores = self._cross_encoder.predict(pairs)

        ranked = sorted(
            zip(candidates, scores),
            key=lambda x: x[1],
            reverse=True,
        )

        top_n = self.config.top_n
        result = []
        for candidate, score in ranked[:top_n]:
            result.append({**candidate, "rerank_score": float(score)})

        return result
