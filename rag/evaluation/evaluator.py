"""RAG evaluation metrics: faithfulness, relevance, precision, and recall."""

import re
from dataclasses import dataclass

import numpy as np

from rag.embeddings.embedder import Embedder


@dataclass
class EvaluationResult:
    faithfulness: float
    answer_relevance: float
    context_precision: float
    context_recall: float

    def __str__(self) -> str:
        return (
            f"Faithfulness:       {self.faithfulness:.3f}\n"
            f"Answer Relevance:   {self.answer_relevance:.3f}\n"
            f"Context Precision:  {self.context_precision:.3f}\n"
            f"Context Recall:     {self.context_recall:.3f}"
        )


class RAGEvaluator:
    """Computes RAG quality metrics without requiring external labeling services."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    def evaluate(
        self,
        query: str,
        answer: str,
        contexts: list[str],
        ground_truth: str = None,
    ) -> EvaluationResult:
        return EvaluationResult(
            faithfulness=self._faithfulness(answer, contexts),
            answer_relevance=self._answer_relevance(query, answer),
            context_precision=self._context_precision(query, contexts),
            context_recall=self._context_recall(query, contexts, ground_truth),
        )

    # ------------------------------------------------------------------
    # Individual metrics
    # ------------------------------------------------------------------

    def _faithfulness(self, answer: str, contexts: list[str]) -> float:
        """Fraction of answer sentences that are semantically grounded in context."""
        sentences = self._split_sentences(answer)
        if not sentences:
            return 1.0

        if not contexts:
            return 0.0

        sentence_embeddings = self.embedder.embed(sentences)
        context_embeddings = self.embedder.embed(contexts)

        grounded = 0
        for sent_vec in sentence_embeddings:
            sims = context_embeddings @ sent_vec  # dot product (vectors normalized)
            if float(sims.max()) >= 0.7:
                grounded += 1

        return grounded / len(sentences)

    def _answer_relevance(self, query: str, answer: str) -> float:
        """Cosine similarity between the query and the generated answer."""
        if not answer.strip():
            return 0.0
        query_vec = self.embedder.embed_query(query)
        answer_vec = self.embedder.embed_query(answer)
        return float(np.dot(query_vec, answer_vec))

    def _context_precision(self, query: str, contexts: list[str]) -> float:
        """Weighted precision: relevant contexts (sim > 0.5) ranked higher score more."""
        if not contexts:
            return 0.0

        query_vec = self.embedder.embed_query(query)
        context_embeddings = self.embedder.embed(contexts)
        sims = (context_embeddings @ query_vec).tolist()

        # Weighted precision@k: sum of (relevant_so_far/k) for each relevant position
        relevant_count = 0
        weighted_sum = 0.0
        for k, sim in enumerate(sims, start=1):
            if sim >= 0.5:
                relevant_count += 1
                weighted_sum += relevant_count / k

        total_relevant = sum(1 for s in sims if s >= 0.5)
        if total_relevant == 0:
            return 0.0

        return weighted_sum / total_relevant

    def _context_recall(
        self, query: str, contexts: list[str], ground_truth: str = None
    ) -> float:
        """Cosine similarity between ground_truth and the best matching context."""
        if ground_truth is None or not ground_truth.strip():
            return 1.0

        if not contexts:
            return 0.0

        gt_vec = self.embedder.embed_query(ground_truth)
        context_embeddings = self.embedder.embed(contexts)
        sims = context_embeddings @ gt_vec
        return float(sims.max())

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        return [s for s in sentences if s.strip()]
