"""Shared fixtures for the ATHENA DQ gateway tests.

The RAG pipeline imports sentence-transformers (torch) at module import time; tests replace it
with a deterministic hash embedder so the gated ingest path runs offline against in-memory Qdrant.
"""
from __future__ import annotations

import hashlib
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")  # Generator() requires one; never called


class _FakeSentenceTransformer:
    DIM = 32

    def __init__(self, *args, **kwargs) -> None:
        pass

    def encode(self, texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True, **kw):
        single = isinstance(texts, str)
        rows = []
        for t in ([texts] if single else texts):
            seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).normal(size=self.DIM)
            rows.append(v / np.linalg.norm(v))
        arr = np.array(rows, dtype=np.float32)
        return arr[0] if single else arr

    def get_sentence_embedding_dimension(self) -> int:
        return self.DIM


class _FakeCrossEncoder:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def predict(self, pairs, **kw):
        return np.zeros(len(pairs), dtype=np.float32)


try:  # use the real package when it is installed
    import sentence_transformers  # noqa: F401
except Exception:  # pragma: no cover - depends on the environment
    stub = types.ModuleType("sentence_transformers")
    stub.SentenceTransformer = _FakeSentenceTransformer
    stub.CrossEncoder = _FakeCrossEncoder
    sys.modules["sentence_transformers"] = stub


@pytest.fixture
def gateway():
    from rag.dq_gateway import DQGateway
    return DQGateway()


@pytest.fixture
def demo(gateway):
    from rag.dq_gateway.demo import seed_demo
    return seed_demo(gateway)
