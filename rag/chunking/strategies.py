"""Text chunking strategies for the RAG pipeline."""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import tiktoken

from rag.config import ChunkingConfig
from rag.ingestion.loaders import Document


@dataclass
class TextChunk:
    text: str
    metadata: dict = field(default_factory=dict)
    chunk_index: int = 0


class ChunkingStrategy:
    """Dispatches to the appropriate chunking strategy."""

    def __init__(self, config: ChunkingConfig) -> None:
        self.config = config
        self._tokenizer = tiktoken.get_encoding("cl100k_base")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def chunk(
        self,
        documents: list[Document],
        embedder=None,
    ) -> list[TextChunk]:
        """Chunk all documents using the configured strategy."""
        all_chunks: list[TextChunk] = []
        global_index = 0

        for doc in documents:
            if self.config.strategy == "fixed":
                chunks = self._fixed_size_chunk(doc.content, doc.metadata)
            elif self.config.strategy == "recursive":
                chunks = self._recursive_chunk(doc.content, doc.metadata)
            elif self.config.strategy == "semantic":
                chunks = self._semantic_chunk(doc.content, doc.metadata, embedder)
            else:
                raise ValueError(f"Unknown chunking strategy: {self.config.strategy}")

            for chunk in chunks:
                chunk.chunk_index = global_index
                global_index += 1
                all_chunks.append(chunk)

        return all_chunks

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _count_tokens(self, text: str) -> int:
        """Count tokens using tiktoken."""
        return len(self._tokenizer.encode(text))

    def _tokens_to_text(self, text: str, start: int, end: int) -> str:
        """Decode a slice of the token sequence back to text."""
        tokens = self._tokenizer.encode(text)
        return self._tokenizer.decode(tokens[start:end])

    def _fixed_size_chunk(
        self, text: str, metadata: dict
    ) -> list[TextChunk]:
        """Split text into fixed-size chunks with overlap (token-based)."""
        tokens = self._tokenizer.encode(text)
        chunk_size = self.config.chunk_size
        overlap = self.config.chunk_overlap
        chunks: list[TextChunk] = []
        start = 0

        while start < len(tokens):
            end = min(start + chunk_size, len(tokens))
            chunk_text = self._tokenizer.decode(tokens[start:end])
            if chunk_text.strip():
                chunks.append(
                    TextChunk(
                        text=chunk_text,
                        metadata={**metadata},
                        chunk_index=len(chunks),
                    )
                )
            if end == len(tokens):
                break
            start += chunk_size - overlap

        return chunks

    def _recursive_chunk(
        self, text: str, metadata: dict
    ) -> list[TextChunk]:
        """Recursively split text by separators, respecting chunk_size."""
        separators = ["\n\n", "\n", ". ", " "]
        raw_chunks = self._split_recursive(text, separators)
        return self._merge_chunks(raw_chunks, metadata)

    def _split_recursive(self, text: str, separators: list[str]) -> list[str]:
        """Split text with the first separator that produces manageable pieces."""
        if self._count_tokens(text) <= self.config.chunk_size:
            return [text] if text.strip() else []

        if not separators:
            # Fall back to fixed splitting if no separators remain
            tokens = self._tokenizer.encode(text)
            result = []
            for i in range(0, len(tokens), self.config.chunk_size):
                piece = self._tokenizer.decode(tokens[i : i + self.config.chunk_size])
                if piece.strip():
                    result.append(piece)
            return result

        sep = separators[0]
        parts = text.split(sep)
        remaining = separators[1:]
        result: list[str] = []

        for part in parts:
            if not part.strip():
                continue
            if self._count_tokens(part) > self.config.chunk_size:
                result.extend(self._split_recursive(part, remaining))
            else:
                result.append(part)

        return result

    def _merge_chunks(
        self, pieces: list[str], metadata: dict
    ) -> list[TextChunk]:
        """Merge small pieces into chunks of up to chunk_size tokens with overlap."""
        chunks: list[TextChunk] = []
        current_pieces: list[str] = []
        current_tokens = 0
        overlap = self.config.chunk_overlap

        for piece in pieces:
            piece_tokens = self._count_tokens(piece)
            if current_tokens + piece_tokens > self.config.chunk_size and current_pieces:
                chunk_text = " ".join(current_pieces)
                if chunk_text.strip():
                    chunks.append(
                        TextChunk(
                            text=chunk_text,
                            metadata={**metadata},
                            chunk_index=len(chunks),
                        )
                    )
                # Carry over overlap tokens from the end
                overlap_text = chunk_text
                overlap_tokens = self._tokenizer.encode(overlap_text)[-overlap:]
                carry = (
                    self._tokenizer.decode(overlap_tokens) if overlap_tokens else ""
                )
                current_pieces = [carry] if carry.strip() else []
                current_tokens = self._count_tokens(carry) if carry.strip() else 0

            current_pieces.append(piece)
            current_tokens += piece_tokens

        if current_pieces:
            chunk_text = " ".join(current_pieces)
            if chunk_text.strip():
                chunks.append(
                    TextChunk(
                        text=chunk_text,
                        metadata={**metadata},
                        chunk_index=len(chunks),
                    )
                )

        return chunks

    def _semantic_chunk(
        self, text: str, metadata: dict, embedder=None
    ) -> list[TextChunk]:
        """Split into sentences, embed each, find semantic breakpoints."""
        if embedder is None:
            # Fall back to recursive chunking if no embedder is available
            return self._recursive_chunk(text, metadata)

        # Split into sentences (naive but effective split)
        import re

        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        sentences = [s for s in sentences if s.strip()]

        if not sentences:
            return []

        if len(sentences) == 1:
            return [TextChunk(text=sentences[0], metadata={**metadata}, chunk_index=0)]

        # Embed all sentences
        embeddings = embedder.embed(sentences)  # shape: (n, dim)

        # Compute cosine distances between adjacent sentences
        threshold = 0.3
        breakpoints: list[int] = []
        for i in range(len(sentences) - 1):
            sim = float(
                np.dot(embeddings[i], embeddings[i + 1])
                / (np.linalg.norm(embeddings[i]) * np.linalg.norm(embeddings[i + 1]) + 1e-10)
            )
            distance = 1.0 - sim
            if distance > threshold:
                breakpoints.append(i + 1)

        # Group sentences into chunks using breakpoints
        groups: list[list[str]] = []
        prev = 0
        for bp in breakpoints:
            groups.append(sentences[prev:bp])
            prev = bp
        groups.append(sentences[prev:])

        # Merge groups that are too small / split groups that are too large
        chunks: list[TextChunk] = []
        for group in groups:
            group_text = " ".join(group)
            if self._count_tokens(group_text) > self.config.chunk_size:
                # Recursively split oversized semantic groups
                sub_chunks = self._recursive_chunk(group_text, metadata)
                chunks.extend(sub_chunks)
            elif group_text.strip():
                chunks.append(
                    TextChunk(
                        text=group_text,
                        metadata={**metadata},
                        chunk_index=len(chunks),
                    )
                )

        # Re-index
        for i, chunk in enumerate(chunks):
            chunk.chunk_index = i

        return chunks
