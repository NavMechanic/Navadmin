"""LLM-based answer generation using Anthropic Claude."""

import os
from dataclasses import dataclass, field

from rag.config import GenerationConfig


@dataclass
class GenerationResult:
    answer: str
    sources: list[dict]
    query: str


class Generator:
    """Constructs augmented prompts and calls Claude to generate grounded answers."""

    PROMPT_TEMPLATE = """\
You are an expert technical assistant. Use ONLY the following pieces of retrieved context to answer the user's question. If the answer cannot be derived from the context, state that the information is missing.

---
RETRIEVED CONTEXT:
{context}
---

USER QUESTION: {question}
ANSWER:"""

    def __init__(self, config: GenerationConfig) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "Export it before running the generator."
            )
        import anthropic

        self.config = config
        self._client = anthropic.Anthropic(api_key=api_key)

    def generate(self, query: str, chunks: list[dict]) -> GenerationResult:
        """Build augmented prompt from chunks and call Claude.

        Args:
            query: The user's natural language question.
            chunks: Retrieved and (optionally) reranked chunks, each a dict
                    with at minimum ``text`` and ``metadata`` keys.

        Returns:
            GenerationResult with the answer, source metadata, and original query.
        """
        context_parts: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            source = chunk.get("metadata", {}).get("source", "unknown")
            context_parts.append(f"[{i}] (source: {source})\n{chunk['text']}")

        context = "\n\n".join(context_parts)
        prompt = self.PROMPT_TEMPLATE.format(context=context, question=query)

        message = self._client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            messages=[{"role": "user", "content": prompt}],
        )

        answer = message.content[0].text

        sources = [
            {"index": i + 1, **chunk.get("metadata", {}), "score": chunk.get("score")}
            for i, chunk in enumerate(chunks)
        ]

        return GenerationResult(answer=answer, sources=sources, query=query)
