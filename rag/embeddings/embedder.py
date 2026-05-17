"""Embedding generation using SentenceTransformers."""

import numpy as np
from sentence_transformers import SentenceTransformer

from rag.config import EmbeddingConfig


class Embedder:
    """Generates and normalizes text embeddings using a SentenceTransformer model."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self._model = SentenceTransformer(config.model_name, device=config.device)

    def embed(self, texts: list[str]) -> np.ndarray:
        """Batch encode texts and return L2-normalized vectors.

        Args:
            texts: List of strings to embed.

        Returns:
            numpy array of shape (len(texts), dimension), normalized.
        """
        vectors = self._model.encode(
            texts,
            batch_size=self.config.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        return np.array(vectors, dtype=np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single query string and return a normalized vector."""
        return self.embed([query])[0]

    @property
    def dimension(self) -> int:
        """Return the dimensionality of the embedding vectors."""
        return self._model.get_sentence_embedding_dimension()
