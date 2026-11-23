"""RAG pipeline package."""

from rag.config import (
    EmbeddingConfig,
    ChunkingConfig,
    VectorStoreConfig,
    RetrievalConfig,
    RerankerConfig,
    GenerationConfig,
    RAGConfig,
)

def __getattr__(name: str):
    # Lazy: importing rag.dq_gateway (used by the API and the twin) must not pull in
    # sentence-transformers/torch. `from rag import RAGPipeline` still works.
    if name in ("RAGPipeline", "QueryResult"):
        from rag import pipeline
        return getattr(pipeline, name)
    raise AttributeError(f"module 'rag' has no attribute {name!r}")


__all__ = [
    "EmbeddingConfig",
    "ChunkingConfig",
    "VectorStoreConfig",
    "RetrievalConfig",
    "RerankerConfig",
    "GenerationConfig",
    "RAGConfig",
    "RAGPipeline",
    "QueryResult",
]
