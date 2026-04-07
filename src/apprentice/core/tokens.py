"""Token estimation for cost tracking across providers."""

from __future__ import annotations

from typing import Any

import tiktoken

_ENCODER = tiktoken.get_encoding("cl100k_base")

# USD per 1M tokens: (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-5": (0.80, 4.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    "claude-opus-4-6": (15.0, 75.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5": (10.0, 30.0),
    "gpt-5-mini": (1.10, 4.40),
}


def count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken cl100k_base encoding.

    Args:
        text: Text to count tokens for.

    Returns:
        Token count.
    """
    return len(_ENCODER.encode(text))


def estimate_cost(
    input_text: str,
    output_text: str,
    model: str,
) -> dict[str, Any]:
    """Estimate token counts and cost for an LLM call.

    Uses tiktoken for accurate token counting and model-specific pricing.
    Falls back to character-based estimation for unknown models.

    Args:
        input_text: The prompt sent to the model.
        output_text: The response from the model.
        model: Model identifier (e.g. "claude-sonnet-4-6", "gpt-5-mini").

    Returns:
        Dict with input_tokens, output_tokens, total_tokens, and cost_usd.
    """
    input_tokens = count_tokens(input_text)
    output_tokens = count_tokens(output_text)
    total_tokens = input_tokens + output_tokens

    # Strip provider prefix for pricing lookup
    model_key = model.split("/")[-1] if "/" in model else model

    pricing = _PRICING.get(model_key)
    if pricing:
        input_rate, output_rate = pricing
        cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    else:
        # Unknown model — use a conservative estimate ($5/M input, $15/M output)
        cost = (input_tokens * 5.0 + output_tokens * 15.0) / 1_000_000

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(cost, 6),
    }
