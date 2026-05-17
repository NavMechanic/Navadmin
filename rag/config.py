"""Dataclass-based configuration for the RAG pipeline."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class EmbeddingConfig:
    model_name: str = "all-MiniLM-L6-v2"
    batch_size: int = 32
    device: str = "cpu"
    dimension: int = 384  # 384 for all-MiniLM-L6-v2; set to 1024 for bge-large


@dataclass
class ChunkingConfig:
    strategy: Literal["fixed", "recursive", "semantic"] = "recursive"
    chunk_size: int = 512
    chunk_overlap: int = 50


@dataclass
class VectorStoreConfig:
    collection_name: str = "navadmin_docs"
    persist_directory: str = ".qdrant_db"
    use_memory: bool = True  # True = in-memory Qdrant, False = persistent on-disk


@dataclass
class RetrievalConfig:
    top_k: int = 25
    use_hybrid: bool = True
    bm25_weight: float = 0.3


@dataclass
class RerankerConfig:
    enabled: bool = True
    model_name: str = "BAAI/bge-reranker-large"
    top_n: int = 5


@dataclass
class GenerationConfig:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 2048
    temperature: float = 0.0


@dataclass
class TelemetryConfig:
    watch_directory: str = "Q:/quality_data/factory_nextest_log"
    delta_threshold_db: float = -3.0  # alert if delta worse than this
    hex_disconnect_value: str = "00000000"
    valid_connection_value: str = "0001;"


@dataclass
class CADConfig:
    default_manifest_path: str = "data/aviator/drawing/aviator_cad_manifest.json"
    vit_model: str = "openai/clip-vit-large-patch14"
    use_vit: bool = False  # False = use text descriptions only (no GPU needed)


@dataclass
class PhysicsConfig:
    voxel_size_mm: float = 1.0
    peak_stress_percentile: float = 95.0


@dataclass
class RAGConfig:
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    cad: CADConfig = field(default_factory=CADConfig)
    physics: PhysicsConfig = field(default_factory=PhysicsConfig)
