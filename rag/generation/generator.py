"""LLM-based answer generation using Anthropic Claude."""

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from rag.config import GenerationConfig

if TYPE_CHECKING:
    from rag.ingestion.telemetry_parser import TelemetryEvent


@dataclass
class GenerationResult:
    answer: str
    sources: list[dict]
    query: str


class Generator:
    """Constructs augmented prompts and calls Claude to generate grounded answers."""

    # Original generic prompt (kept for backward compatibility)
    PROMPT_TEMPLATE = """\
You are an expert technical assistant. Use ONLY the following pieces of retrieved context to answer the user's question. If the answer cannot be derived from the context, state that the information is missing.

---
RETRIEVED CONTEXT:
{context}
---

USER QUESTION: {question}
ANSWER:"""

    # Factory-grade RCA prompt for manufacturing failure analysis
    FACTORY_RCA_PROMPT = """\
[SYSTEM INTERACTION MANDATE] You are the lead Process Integration Expert for Padget Electronics FATP lines. \
Analyze the incoming testing failure using ONLY the cross-aligned material, design, and process context blocks \
provided below. Do not generate information outside these boundaries.

---
1. INCOMING TESTER ARTIFACT (SFC SERVER):
{tester_artifact}

2. RETRIEVED ENGINEERING CONTEXT:
{context}
---

USER TARGET QUERY: {question}
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

    def _format_tester_artifact(
        self, event: Optional["TelemetryEvent"]
    ) -> str:
        """Format a TelemetryEvent as a structured SFC server artifact block."""
        if event is None:
            return "Natural language query — no live SFC artifact attached."

        lines = [
            f"Track ID:          {event.track_id}",
            f"Process:           {event.process}",
            f"Station:           {event.station}" if event.station else "",
            f"Test Code:         {event.test_code}",
            f"Short Code:        {event.test_code_short}" if event.test_code_short else "",
            f"Result:            FAIL",
        ]

        if event.test_value is not None:
            lines.append(f"Test Value:        {event.test_value}")

        ll_str = str(event.lower_limit) if event.lower_limit is not None else "N/A"
        ul_str = str(event.upper_limit) if event.upper_limit is not None else "N/A"
        if event.lower_limit is not None or event.upper_limit is not None:
            lines.append(f"Limits [LL, UL]:   [{ll_str}, {ul_str}]")

        if event.delta is not None:
            sign = "+" if event.delta > 0 else ""
            lines.append(f"Delta from limit:  {sign}{event.delta:.4f}")

        if event.fail_desc:
            lines.append(f"Failure Desc:      {event.fail_desc}")

        if event.ntf:
            lines.append("NTF:               No-Trouble-Found on retest")

        if event.communication_state:
            lines.append(f"Comm State:        {event.communication_state}")

        return "\n".join(l for l in lines if l)

    def generate(
        self,
        query: str,
        chunks: list[dict],
        telemetry_event: Optional["TelemetryEvent"] = None,
    ) -> GenerationResult:
        """Build augmented prompt from chunks and call Claude.

        When a TelemetryEvent is provided, uses the factory-grade RCA prompt
        template with a structured SFC server artifact block.

        Args:
            query: The user's natural language question.
            chunks: Retrieved and (optionally) reranked chunks, each a dict
                    with at minimum ``text`` and ``metadata`` keys.
            telemetry_event: Optional structured failure event from NexTest tester.

        Returns:
            GenerationResult with the answer, source metadata, and original query.
        """
        context_parts: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            source = chunk.get("metadata", {}).get("source", "unknown")
            context_parts.append(f"[{i}] (source: {source})\n{chunk['text']}")

        context = "\n\n".join(context_parts)

        if telemetry_event is not None:
            tester_artifact = self._format_tester_artifact(telemetry_event)
            prompt = self.FACTORY_RCA_PROMPT.format(
                tester_artifact=tester_artifact,
                context=context,
                question=query,
            )
        else:
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
