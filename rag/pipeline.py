"""End-to-end RAG pipeline orchestrator."""

from dataclasses import dataclass
from typing import Optional

from rag.chunking.strategies import ChunkingStrategy
from rag.config import RAGConfig
from rag.embeddings.embedder import Embedder
from rag.evaluation.evaluator import EvaluationResult, RAGEvaluator
from rag.generation.generator import GenerationResult, Generator
from rag.ingestion.loaders import DocumentLoader
from rag.reranking.reranker import Reranker
from rag.retrieval.retriever import Retriever
from rag.vectorstore.store import VectorStore


@dataclass
class QueryResult:
    answer: str
    sources: list[dict]
    evaluation: Optional[EvaluationResult] = None


class RAGPipeline:
    """Connects ingestion, retrieval, reranking, generation, and evaluation."""

    def __init__(self, config: RAGConfig = None) -> None:
        self.config = config or RAGConfig()

        self._loader = DocumentLoader()
        self._embedder = Embedder(self.config.embedding)
        self._chunker = ChunkingStrategy(self.config.chunking)
        self._vector_store = VectorStore(self.config.vector_store, self._embedder)
        self._retriever = Retriever(self._vector_store, self._embedder, self.config.retrieval)
        self._reranker = Reranker(self.config.reranker)
        self._generator = Generator(self.config.generation)
        self._evaluator = RAGEvaluator(self._embedder)

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest(self, source: str) -> int:
        """Load, chunk, embed, and index documents from a file or directory.

        Args:
            source: Path to a file or directory.

        Returns:
            Number of chunks stored in the vector index.
        """
        import os

        if os.path.isdir(source):
            documents = self._loader.load_directory(source)
        else:
            documents = self._loader._load_file(source)

        embedder = self._embedder if self.config.chunking.strategy == "semantic" else None
        chunks = self._chunker.chunk(documents, embedder=embedder)

        self._vector_store.add_chunks(chunks)
        self._retriever.index_for_bm25(chunks)

        return len(chunks)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        question: str,
        evaluate: bool = False,
        ground_truth: str = None,
    ) -> QueryResult:
        """Retrieve, rerank, generate, and optionally evaluate an answer.

        Args:
            question: The user's natural language question.
            evaluate: Whether to compute evaluation metrics.
            ground_truth: Optional reference answer for context recall scoring.

        Returns:
            QueryResult with the answer, sources, and optional evaluation metrics.
        """
        candidates = self._retriever.retrieve(question)
        reranked = self._reranker.rerank(question, candidates)
        generation_result: GenerationResult = self._generator.generate(question, reranked)

        eval_result: Optional[EvaluationResult] = None
        if evaluate:
            contexts = [c["text"] for c in reranked]
            eval_result = self._evaluator.evaluate(
                query=question,
                answer=generation_result.answer,
                contexts=contexts,
                ground_truth=ground_truth,
            )

        return QueryResult(
            answer=generation_result.answer,
            sources=generation_result.sources,
            evaluation=eval_result,
        )

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def reset_index(self) -> None:
        """Clear the vector store and BM25 index."""
        self._vector_store.reset()
        self._retriever._bm25_corpus = []
        self._retriever._bm25_ids = []
        self._retriever._bm25 = None
