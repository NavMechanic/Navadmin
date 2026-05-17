"""End-to-end RAG pipeline orchestrator."""

import os
from dataclasses import dataclass
from typing import Optional

from rag.chunking.strategies import ChunkingStrategy
from rag.config import RAGConfig
from rag.embeddings.embedder import Embedder
from rag.evaluation.evaluator import EvaluationResult, RAGEvaluator
from rag.generation.generator import GenerationResult, Generator
from rag.ingestion.loaders import Document, DocumentLoader
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
    # Shared helpers
    # ------------------------------------------------------------------

    def _chunk_and_index_documents(self, documents: list[Document]) -> int:
        """Chunk a list of Documents, embed, and index them. Returns chunk count."""
        embedder = self._embedder if self.config.chunking.strategy == "semantic" else None
        chunks = self._chunker.chunk(documents, embedder=embedder)
        self._vector_store.add_chunks(chunks)
        self._retriever.index_for_bm25(chunks)
        return len(chunks)

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
        if os.path.isdir(source):
            documents = self._loader.load_directory(source)
        else:
            documents = self._loader._load_file(source)

        return self._chunk_and_index_documents(documents)

    def ingest_telemetry(self, source: str) -> int:
        """Parse NexTest log files and index as structured failure events.

        Args:
            source: Path to a .log/.txt file or directory containing log files.

        Returns:
            Number of chunks stored in the vector index.
        """
        from rag.ingestion.telemetry_parser import TelemetryParser

        parser = TelemetryParser()
        if os.path.isdir(source):
            events = parser.parse_directory(source)
        else:
            events = parser.parse_log_file(source)

        documents = parser.events_to_documents(events)
        return self._chunk_and_index_documents(documents)

    def ingest_fea(self, source: str) -> int:
        """Parse FEA mesh JSON/CSV and index stress voxels.

        Args:
            source: Path to a .json or .csv FEA mesh file.

        Returns:
            Number of chunks stored in the vector index.
        """
        from rag.ingestion.physics_parser import FEAVoxelizer

        voxelizer = FEAVoxelizer()
        if source.endswith(".json"):
            nodes = voxelizer.parse_json_mesh(source)
        else:
            nodes = voxelizer.parse_csv_mesh(source)

        documents = voxelizer.nodes_to_documents(nodes)
        return self._chunk_and_index_documents(documents)

    def ingest_cad(self, source: str) -> int:
        """Parse STEP file or CAD JSON manifest and index components.

        Args:
            source: Path to a .json manifest or .stp/.step STEP file.

        Returns:
            Number of chunks stored in the vector index.
        """
        from rag.ingestion.cad_parser import STEPParser

        parser = STEPParser()
        if source.endswith(".json"):
            components = parser.parse_json_manifest(source)
        else:
            components = parser.parse_step_text(source)

        documents = parser.components_to_documents(components)
        return self._chunk_and_index_documents(documents)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        question: str,
        evaluate: bool = False,
        ground_truth: str = None,
        spatial_bbox: dict = None,
        telemetry_event=None,
    ) -> QueryResult:
        """Retrieve, rerank, generate, and optionally evaluate an answer.

        Args:
            question: The user's natural language question.
            evaluate: Whether to compute evaluation metrics.
            ground_truth: Optional reference answer for context recall scoring.
            spatial_bbox: Optional spatial bounding box dict for spatial search.
                          Format: {"x_axis_range": [min, max], "y_axis_range": [...],
                                   "z_axis_range": [...]}
            telemetry_event: Optional TelemetryEvent for factory RCA prompt.

        Returns:
            QueryResult with the answer, sources, and optional evaluation metrics.
        """
        query_vector = self._embedder.embed_query(question)

        if spatial_bbox:
            candidates = self._vector_store.spatial_search(
                query_vector, bbox=spatial_bbox, top_k=self.config.retrieval.top_k
            )
        else:
            candidates = self._retriever.retrieve(question)

        reranked = self._reranker.rerank(question, candidates)

        generation_result: GenerationResult = self._generator.generate(
            question, reranked, telemetry_event=telemetry_event
        )

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
