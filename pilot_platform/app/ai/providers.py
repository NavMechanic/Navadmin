"""LLM providers behind the gateway.

* `local`     — deterministic, offline, zero-cost. Used for development and tests.
* `anthropic` — Claude via the official `anthropic` SDK (optional dependency). Credentials
                resolve the SDK's usual way (ANTHROPIC_API_KEY, or an `ant auth login` profile).
                Server-side refusal fallbacks are enabled (`fallbacks="default"`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from ..core.errors import ProviderError

# USD per million tokens (input, output). Haiku 5.5 bills a higher rate above 100K input tokens.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
}
LONG_CONTEXT_PRICES = {"claude-haiku-5-5": (100_000, Decimal("0.50"), Decimal("2.50"))}


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def cost_usd(model: str, input_tokens: int, output_tokens: int, provider: str = "anthropic") -> Decimal:
    if provider == "local":
        return Decimal("0")
    if model not in PRICES:
        raise ProviderError(f"no price configured for model {model!r}; add it to PRICES before routing to it")
    price_in, price_out = PRICES[model]
    tier = LONG_CONTEXT_PRICES.get(model)
    if tier and input_tokens > tier[0]:
        price_in, price_out = tier[1], tier[2]
    return (price_in * input_tokens + price_out * output_tokens) / Decimal(1_000_000)


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str


class LocalProvider:
    """Deterministic stand-in: echoes a digest of the request. Never calls the network."""
    name = "local"

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int, effort: str) -> Completion:
        first_line = prompt.strip().splitlines()[0][:160] if prompt.strip() else ""
        docs = prompt.count("<document ")
        text = f"[local:{model}] {len(prompt)} chars, {docs} retrieved documents. First line: {first_line}"
        return Completion(text, estimate_tokens(system + prompt), estimate_tokens(text), model, "end_turn")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise ProviderError("anthropic SDK not installed: pip install anthropic") from exc
        self._sdk = anthropic
        self._client = anthropic.Anthropic()

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int, effort: str) -> Completion:
        sdk = self._sdk
        try:
            resp = self._client.beta.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except sdk.RateLimitError as exc:
            raise ProviderError("provider rate limited", retryable=True, status=429) from exc
        except sdk.APIStatusError as exc:
            raise ProviderError(f"provider error {exc.status_code}", retryable=exc.status_code >= 500,
                                status=exc.status_code) from exc
        except sdk.APIConnectionError as exc:
            raise ProviderError("provider unreachable", retryable=True) from exc
        text = "".join(block.text for block in resp.content if block.type == "text")
        return Completion(text, resp.usage.input_tokens, resp.usage.output_tokens, resp.model, resp.stop_reason)


_PROVIDERS: dict[str, object] = {}


def get_provider(name: str):
    if name not in ("local", "anthropic"):
        raise ProviderError(f"unknown provider {name!r}")
    if name not in _PROVIDERS:
        _PROVIDERS[name] = LocalProvider() if name == "local" else AnthropicProvider()
    return _PROVIDERS[name]
