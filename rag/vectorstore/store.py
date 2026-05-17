"""ChromaDB-backed vector store with cosine similarity."""

import numpy as np
import chromadb
from chromadb.config import Settings

from rag.chunking.strategies import TextChunk
from rag.config import VectorStoreConfig
from rag.embeddings.embedder import Embedder


class VectorStore:
    """Persistent ChromaDB vector store using precomputed embeddings."""

    def __init__(self, config: VectorStoreConfig, embedder: Embedder) -> None:
        self.config = config
        self.embedder = embedder

        self._client = chromadb.PersistentClient(
            path=config.persist_directory,
        )
        self._collection = self._get_or_create_collection()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_collection(self):
        return self._client.get_or_create_collection(
            name=self.config.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def _make_id(self, chunk_index: int) -> str:
        return f"chunk_{chunk_index}"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add_chunks(self, chunks: list[TextChunk]) -> None:
        """Embed all chunks and add them to the collection."""
        if not chunks:
            return

        texts = [chunk.text for chunk in chunks]
        embeddings = self.embedder.embed(texts)  # shape: (n, dim)

        ids = [self._make_id(chunk.chunk_index) for chunk in chunks]
        metadatas = [chunk.metadata for chunk in chunks]

        # Upsert in batches to avoid ChromaDB limits
        batch_size = 500
        for start in range(0, len(chunks), batch_size):
            end = start + batch_size
            self._collection.upsert(
                ids=ids[start:end],
                embeddings=embeddings[start:end].tolist(),
                documents=texts[start:end],
                metadatas=metadatas[start:end],
            )

    def similarity_search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
    ) -> list[dict]:
        """Query the collection and return results with similarity scores.

        ChromaDB cosine space returns distances in [0, 2]; we convert to
        similarity as score = 1 - distance (clipped to [0, 1]).
        """
        results = self._collection.query(
            query_embeddings=[query_vector.tolist()],
            n_results=min(top_k, self.get_collection_count() or 1),
            include=["documents", "metadatas", "distances"],
        )

        output: list[dict] = []
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        ids = results.get("ids", [[]])[0]

        for doc_id, text, meta, dist in zip(ids, documents, metadatas, distances):
            score = max(0.0, 1.0 - dist)  # cosine distance -> similarity
            output.append(
                {
                    "id": doc_id,
                    "text": text,
                    "metadata": meta,
                    "score": score,
                }
            )

        return output

    def get_collection_count(self) -> int:
        """Return the number of documents in the collection."""
        return self._collection.count()

    def reset(self) -> None:
        """Delete and recreate the collection."""
        self._client.delete_collection(self.config.collection_name)
        self._collection = self._get_or_create_collection()
