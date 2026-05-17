"""Qdrant-backed vector store with cosine similarity and spatial filtering."""

import uuid
import warnings
from typing import Any, Optional

import numpy as np

from rag.chunking.strategies import TextChunk
from rag.config import VectorStoreConfig
from rag.embeddings.embedder import Embedder

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        HnswConfigDiff,
        MatchValue,
        PointStruct,
        Range,
        VectorParams,
    )
    _QDRANT_AVAILABLE = True
except ImportError:
    _QDRANT_AVAILABLE = False

# qdrant-client >=1.10 removed .search() in favour of .query_points()
_USE_QUERY_POINTS = False
if _QDRANT_AVAILABLE:
    try:
        from qdrant_client import QdrantClient as _QC
        _USE_QUERY_POINTS = hasattr(_QC, "query_points")
    except Exception:
        pass


def _require_qdrant() -> None:
    if not _QDRANT_AVAILABLE:
        raise ImportError(
            "qdrant-client is required for VectorStore. "
            "Install it with: pip install 'qdrant-client>=1.9.0'"
        )


def _sanitize_payload(meta: dict) -> dict:
    """Convert metadata values to Qdrant-compatible scalar types."""
    result: dict = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, (bool, int, float, str)):
            result[k] = v
        elif isinstance(v, dict):
            # Flatten nested dicts with dot-notation keys
            for sub_k, sub_v in v.items():
                if isinstance(sub_v, (bool, int, float, str, list)):
                    result[f"{k}__{sub_k}"] = sub_v
        elif isinstance(v, list):
            # Convert list to comma-separated string if not scalar list
            if all(isinstance(i, (int, float)) for i in v):
                result[k] = v
            else:
                result[k] = ", ".join(str(i) for i in v)
        else:
            result[k] = str(v)
    return result


class VectorStore:
    """Qdrant vector store using precomputed embeddings with HNSW index."""

    BATCH_SIZE = 100

    def __init__(self, config: VectorStoreConfig, embedder: Embedder) -> None:
        _require_qdrant()

        self.config = config
        self.embedder = embedder

        # Determine vector dimension
        self._dim = getattr(embedder, "dimension", None) or embedder.embed(["test"]).shape[1]

        # Connect to Qdrant
        if config.use_memory:
            self._client = QdrantClient(":memory:")
        else:
            self._client = QdrantClient(path=config.persist_directory)

        self._ensure_collection()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_collection(self) -> None:
        """Create the collection if it does not exist."""
        existing = [c.name for c in self._client.get_collections().collections]
        if self.config.collection_name not in existing:
            self._client.create_collection(
                collection_name=self.config.collection_name,
                vectors_config=VectorParams(
                    size=self._dim,
                    distance=Distance.COSINE,
                ),
                hnsw_config=HnswConfigDiff(m=16, ef_construct=100),
            )

    def _make_point_id(self) -> str:
        """Generate a unique UUID string for a Qdrant point."""
        return str(uuid.uuid4())

    def _results_to_dicts(self, results) -> list[dict]:
        """Convert Qdrant ScoredPoint objects to plain dicts.

        Accepts either a list of ScoredPoint or a QueryResponse object
        (qdrant-client >=1.10 query_points returns QueryResponse).
        """
        # QueryResponse wraps the list in a .points attribute
        if hasattr(results, "points"):
            results = results.points

        output: list[dict] = []
        for hit in results:
            payload = dict(hit.payload or {})
            text = payload.pop("text", "")
            output.append(
                {
                    "id": str(hit.id),
                    "text": text,
                    "metadata": payload,
                    "score": float(hit.score),
                }
            )
        return output

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add_chunks(self, chunks: list[TextChunk]) -> None:
        """Embed all chunks and add them to the Qdrant collection.

        Args:
            chunks: List of TextChunk objects to index.
        """
        if not chunks:
            return

        texts = [chunk.text for chunk in chunks]
        embeddings = self.embedder.embed(texts)  # shape: (n, dim)

        points: list[PointStruct] = []
        for i, chunk in enumerate(chunks):
            payload = _sanitize_payload(chunk.metadata)
            payload["text"] = chunk.text
            payload["chunk_index"] = chunk.chunk_index

            points.append(
                PointStruct(
                    id=self._make_point_id(),
                    vector=embeddings[i].tolist(),
                    payload=payload,
                )
            )

        # Upsert in batches
        for start in range(0, len(points), self.BATCH_SIZE):
            batch = points[start : start + self.BATCH_SIZE]
            self._client.upsert(
                collection_name=self.config.collection_name,
                points=batch,
            )

    def _vector_search(
        self,
        query_vector: np.ndarray,
        limit: int,
        qdrant_filter=None,
    ):
        """Internal: dispatch to query_points (qdrant >=1.10) or legacy search."""
        kwargs = dict(
            collection_name=self.config.collection_name,
            query=query_vector.tolist(),
            limit=limit,
            with_payload=True,
            query_filter=qdrant_filter,
        )
        return self._client.query_points(**kwargs)

    def similarity_search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        filters: Optional[dict] = None,
    ) -> list[dict]:
        """Query the collection and return results with similarity scores.

        Args:
            query_vector: Dense query embedding.
            top_k: Number of results to return.
            filters: Optional Qdrant Filter object or dict of field:value pairs.

        Returns:
            List of dicts with id, text, metadata, and score keys.
        """
        count = self.get_collection_count()
        if count == 0:
            return []

        limit = min(top_k, count)
        qdrant_filter = self._build_filter(filters) if filters else None
        results = self._vector_search(query_vector, limit, qdrant_filter)
        return self._results_to_dicts(results)

    def spatial_search(
        self,
        query_vector: np.ndarray,
        bbox: dict,
        top_k: int = 10,
    ) -> list[dict]:
        """Spatial bounding box search combined with vector similarity.

        Args:
            query_vector: Dense query embedding.
            bbox: Dict with x_axis_range, y_axis_range, z_axis_range keys,
                  each being [min, max] float pairs.
            top_k: Number of results to return.

        Returns:
            List of dicts with id, text, metadata, and score keys.
        """
        count = self.get_collection_count()
        if count == 0:
            return []

        conditions = []

        if "x_axis_range" in bbox and bbox["x_axis_range"]:
            x_min, x_max = bbox["x_axis_range"]
            conditions.append(FieldCondition(key="x_min", range=Range(gte=float(x_min))))
            conditions.append(FieldCondition(key="x_max", range=Range(lte=float(x_max))))

        if "y_axis_range" in bbox and bbox["y_axis_range"]:
            y_min, y_max = bbox["y_axis_range"]
            conditions.append(FieldCondition(key="y_min", range=Range(gte=float(y_min))))
            conditions.append(FieldCondition(key="y_max", range=Range(lte=float(y_max))))

        if "z_axis_range" in bbox and bbox["z_axis_range"]:
            z_min, z_max = bbox["z_axis_range"]
            conditions.append(FieldCondition(key="z_min", range=Range(gte=float(z_min))))
            conditions.append(FieldCondition(key="z_max", range=Range(lte=float(z_max))))

        qdrant_filter = Filter(must=conditions) if conditions else None
        results = self._vector_search(query_vector, min(top_k, count), qdrant_filter)
        return self._results_to_dicts(results)

    def filter_by_metadata(
        self, field: str, value: str, top_k: int = 10
    ) -> list[dict]:
        """Pure metadata filter using Qdrant scroll (no vector needed).

        Args:
            field: Payload field name to filter on.
            value: Value to match exactly.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with id, text, metadata keys.
        """
        qdrant_filter = Filter(
            must=[FieldCondition(key=field, match=MatchValue(value=value))]
        )

        results, _ = self._client.scroll(
            collection_name=self.config.collection_name,
            scroll_filter=qdrant_filter,
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )

        output: list[dict] = []
        for point in results:
            payload = point.payload or {}
            text = payload.pop("text", "")
            output.append(
                {
                    "id": str(point.id),
                    "text": text,
                    "metadata": payload,
                    "score": 1.0,
                }
            )
        return output

    def get_collection_count(self) -> int:
        """Return the number of points in the collection."""
        info = self._client.get_collection(self.config.collection_name)
        return info.points_count or 0

    def reset(self) -> None:
        """Delete and recreate the collection."""
        self._client.delete_collection(self.config.collection_name)
        self._ensure_collection()

    # ------------------------------------------------------------------
    # Private filter builder
    # ------------------------------------------------------------------

    def _build_filter(self, filters: Any) -> Optional[Filter]:
        """Build a Qdrant Filter from a dict or return an existing Filter."""
        if filters is None:
            return None
        if isinstance(filters, Filter):
            return filters
        if isinstance(filters, dict):
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v))
                for k, v in filters.items()
            ]
            return Filter(must=conditions) if conditions else None
        warnings.warn(f"VectorStore: unsupported filter type {type(filters)}, ignoring")
        return None
