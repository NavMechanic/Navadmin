"""End-to-end RAG pipeline orchestrator.

Every ingestion path is gated by the ATHENA Data Quality Gateway: documents reach Qdrant and the
BM25 index only through ``_chunk_and_index_documents``, which refuses to index anything whose
Canonical I-O Manifest is not RELEASED (or, in sandbox mode, HOLD + origin SYNTHETIC).
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

from rag.chunking.strategies import ChunkingStrategy
from rag.config import RAGConfig
from rag.dq_gateway import ArtifactSubmission, DQGateRejected, DQGateway, GateDecision, sha256_bytes
from rag.dq_gateway.sources import read_kind
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


DQMode = Literal["enforce", "sandbox"]


def _json_safe(meta: dict) -> dict:
    return {k: (v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)) for k, v in meta.items()}


class RAGPipeline:
    """Connects ingestion, retrieval, reranking, generation, and evaluation."""

    def __init__(
        self,
        config: RAGConfig = None,
        dq_gateway: Optional[DQGateway] = None,
        dq_mode: DQMode = "enforce",
        tenant_id: Optional[str] = None,
        actor_id: str = "rag-pipeline",
    ) -> None:
        self.config = config or RAGConfig()

        # ATHENA Data Quality Gateway — nothing is indexed without a gate decision.
        if dq_mode not in ("enforce", "sandbox"):
            raise ValueError("dq_mode must be 'enforce' or 'sandbox'")
        self.dq_gateway = dq_gateway or DQGateway()
        self.dq_mode: DQMode = dq_mode
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.dq_decisions: list[GateDecision] = []
        # Optional resolver used by the PDF/Drive paths: filename -> ArtifactSubmission
        self.dq_submission_for: Optional[Callable[[str], Optional[ArtifactSubmission]]] = None

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

    def _dq_admit(
        self,
        documents: list[Document],
        submission: Optional[ArtifactSubmission],
        source_kind: str,
        content: Optional[bytes],
        file_bytes: dict[str, bytes],
    ) -> GateDecision:
        """Run the gateway on the parsed corpus. Returns the decision or raises DQGateRejected."""
        if submission is None:
            raise DQGateRejected("DQ gateway: no Canonical I-O Manifest supplied; nothing is indexed without one")
        if not self.tenant_id:
            raise DQGateRejected("DQ gateway: RAGPipeline needs tenant_id to enforce tenant isolation (DQ09)")
        if submission.payload:
            raise ValueError("submission.payload must be empty: the pipeline builds the document_corpus.v1 "
                             "payload from what it actually parsed")
        payload = {
            "source_kind": source_kind,
            "files": [{"name": n, "sha256": sha256_bytes(b), "format": Path(n).suffix.lstrip(".").lower() or "bin",
                       "size_bytes": len(b)} for n, b in sorted(file_bytes.items())],
            "documents": [{"content": d.content, "metadata": _json_safe(d.metadata)} for d in documents],
            "document_count": len(documents),
        }
        gated = submission.model_copy(update={"payload": payload, "content": content, "files": file_bytes,
                                              "transport": "FILE"})
        decision = self.dq_gateway.submit(gated, self.tenant_id, self.actor_id)
        self.dq_decisions.append(decision)
        if decision.quality_gate == "RELEASED":
            return decision
        if self.dq_mode == "sandbox" and decision.quality_gate == "HOLD" and decision.origin_type == "SYNTHETIC":
            return decision
        failed = ", ".join(f"{r.rule_id} {r.error_code}" for r in decision.failed_rules) or "; ".join(decision.reasons)
        raise DQGateRejected(
            f"DQ gateway: {decision.artifact_ref or decision.event_id} is {decision.quality_gate} — not indexed ({failed})",
            decision,
        )

    @staticmethod
    def _dq_tags(decision: GateDecision) -> dict:
        """Chunk metadata written to Qdrant so retrieval can tell released field evidence from sandbox data."""
        released = decision.quality_gate == "RELEASED"
        return {
            "dq_event_id": decision.event_id,
            "dq_quality_gate": decision.quality_gate,
            "dq_origin_type": decision.origin_type,
            "dq_node_id": decision.node_id,
            "dq_artifact_ref": decision.artifact_ref,
            "dq_output_sha256": (decision.evaluated_manifest or {}).get("output_file_sha256"),
            "dq_rights": (decision.evaluated_manifest or {}).get("source_license_rights"),
            "dq_field_evidence": released and decision.origin_type == "REAL",
            "dq_sandbox": not released,
        }

    def _chunk_and_index_documents(
        self,
        documents: list[Document],
        submission: Optional[ArtifactSubmission] = None,
        *,
        source_kind: str = "document",
        content: Optional[bytes] = None,
        file_bytes: Optional[dict[str, bytes]] = None,
    ) -> int:
        """Gate, then chunk, embed, and index. Returns chunk count.

        This is the only path into the vector store and BM25 index. Raises DQGateRejected
        (and indexes nothing) unless the gateway admits the artifact.
        """
        decision = self._dq_admit(documents, submission, source_kind, content, file_bytes or {})
        tags = self._dq_tags(decision)
        gated_docs = [Document(content=d.content, metadata={**d.metadata, **tags}) for d in documents]
        embedder = self._embedder if self.config.chunking.strategy == "semantic" else None
        chunks = self._chunker.chunk(gated_docs, embedder=embedder)
        self._vector_store.add_chunks(chunks)
        self._retriever.index_for_bm25(chunks)
        return len(chunks)

    def ingest_documents(
        self,
        documents: list[Document],
        submission: Optional[ArtifactSubmission],
        *,
        source_kind: str,
        content: Optional[bytes],
        file_bytes: dict[str, bytes],
    ) -> int:
        """Gated entry for callers that load documents themselves (Drive mirror, connectors).

        ``content``/``file_bytes`` must be the bytes the documents were parsed from
        (see ``rag.dq_gateway.sources``); without them DQ05 quarantines the artifact.
        """
        return self._chunk_and_index_documents(documents, submission, source_kind=source_kind,
                                               content=content, file_bytes=file_bytes)

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest(self, source: str, submission: Optional[ArtifactSubmission] = None) -> int:
        """Load, chunk, embed, and index documents from a file or directory.

        Args:
            source: Path to a file or directory.
            submission: Canonical I-O Manifest (output_schema ``document_corpus.v1``) whose
                ``output_file_sha256`` is the SHA-256 of the file, or of ``tree_bytes(dir)``.

        Returns:
            Number of chunks stored in the vector index.

        Raises:
            DQGateRejected: if the gateway does not admit the artifact (nothing is indexed).
        """
        if os.path.isdir(source):
            documents = self._loader.load_directory(source)
        else:
            documents = self._loader._load_file(source)

        content, files = read_kind(source, "document")
        return self._chunk_and_index_documents(documents, submission, source_kind="document",
                                               content=content, file_bytes=files)

    def ingest_telemetry(self, source: str, submission: Optional[ArtifactSubmission] = None) -> int:
        """Parse NexTest log files and index as structured failure events.

        Args:
            source: Path to a .log/.txt file or directory containing log files.
            submission: Canonical I-O Manifest for the log file/bundle (see ``ingest``).

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
        content, files = read_kind(source, "telemetry_log")
        return self._chunk_and_index_documents(documents, submission, source_kind="telemetry_log",
                                               content=content, file_bytes=files)

    def ingest_fea(self, source: str, submission: Optional[ArtifactSubmission] = None) -> int:
        """Parse FEA mesh JSON/CSV and index stress voxels.

        Args:
            source: Path to a .json or .csv FEA mesh file.
            submission: Canonical I-O Manifest for the mesh file (see ``ingest``).

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
        content, files = read_kind(source, "fea_mesh")
        return self._chunk_and_index_documents(documents, submission, source_kind="fea_mesh",
                                               content=content, file_bytes=files)

    def ingest_cad(self, source: str, submission: Optional[ArtifactSubmission] = None) -> int:
        """Parse STEP file or CAD JSON manifest and index components.

        Args:
            source: Path to a .json manifest or .stp/.step STEP file.
            submission: Canonical I-O Manifest for the CAD file (see ``ingest``). At node N3,
                DQ17 requires a native, hash-verified design file (.step/.kicad_*), so a JSON
                manifest alone is sent back for REWORK.

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
        content, files = read_kind(source, "cad")
        return self._chunk_and_index_documents(documents, submission, source_kind="cad",
                                               content=content, file_bytes=files)

    def ingest_pdf(
        self,
        source: str,
        submission: Optional[ArtifactSubmission] = None,
        submissions: Optional[dict[str, ArtifactSubmission]] = None,
    ) -> int:
        """Ingest a single PDF or directory of PDFs with section/table-aware extraction.

        Args:
            source: Path to a .pdf file or directory containing PDFs.
            submission: Manifest for a single PDF.
            submissions: For a directory, one manifest per PDF filename. PDFs without one are
                rejected by the gateway and reported as errors; nothing about them is indexed.

        Returns:
            Number of chunks stored in the vector index.
        """
        from rag.ingestion.pdf_pipeline import PDFPipeline

        if os.path.isdir(source):
            resolver = lambda path: (submissions or {}).get(Path(path).name)  # noqa: E731
        else:
            resolver = lambda path: submission  # noqa: E731
        pdf_pipeline = PDFPipeline(self, submission_for=resolver)
        if os.path.isdir(source):
            results = pdf_pipeline.ingest_local_directory(source)
        else:
            results = [pdf_pipeline.ingest_file(source)]

        return sum(r.chunk_count for r in results if r.success)

    def ingest_pdf_drive(
        self,
        folder_id: str = None,
        credentials_path: str = None,
        local_mirror: str = None,
        manifest_path: str = "data/.drive_manifest.json",
        folders: list = None,
    ) -> list:
        """Sync and ingest PDFs from Google Drive Aviator folders.

        Supports incremental mode via a JSON manifest file — files already
        indexed at the same Drive modified_time are skipped.

        Args:
            folder_id: Specific Drive folder ID (overrides Aviator defaults).
            credentials_path: Path to service-account JSON.
            local_mirror: Path to a local directory mirroring Drive (offline mode).
            manifest_path: Path to the sync manifest JSON file.
            folders: Subset of Aviator folder names to sync (default: all).

        Returns:
            List of PDFIngestionResult objects.
        """
        from rag.ingestion.drive_sync import DrivePDFSync

        sync = DrivePDFSync(
            rag_pipeline=self,
            credentials_path=credentials_path,
            manifest_path=manifest_path,
        )

        if local_mirror:
            return sync.sync_local_mirror(local_mirror)
        elif folder_id:
            return sync.sync_folder(folder_id)
        else:
            return sync.sync_all_aviator(folders=folders)

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
