"""Dataclass-based configuration for the RAG pipeline."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class EmbeddingConfig:
    model_name: str = "all-MiniLM-L6-v2"
    batch_size: int = 32
    device: str = "cpu"


@dataclass
class ChunkingConfig:
    strategy: Literal["fixed", "recursive", "semantic"] = "recursive"
    chunk_size: int = 512
    chunk_overlap: int = 50


@dataclass
class VectorStoreConfig:
    collection_name: str = "navadmin_docs"
    persist_directory: str = ".chroma_db"


@dataclass
class RetrievalConfig:
    top_k: int = 25
    use_hybrid: bool = True
    bm25_weight: float = 0.3


@dataclass
class RerankerConfig:
    enabled: bool = True
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    top_n: int = 5


@dataclass
class GenerationConfig:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 2048
    temperature: float = 0.0


@dataclass
class RAGConfig:
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
