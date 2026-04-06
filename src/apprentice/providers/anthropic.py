"""Anthropic provider — Claude API integration."""

from __future__ import annotations

import os
from typing import Any, Literal

import anthropic

from apprentice.providers.base import Completion

# USD cost per token by model
_INPUT_COST: dict[str, float] = {
    "claude-sonnet-4-20250514": 3.0 / 1_000_000,
    "claude-haiku-4-5-20251001": 0.80 / 1_000_000,
}
_OUTPUT_COST: dict[str, float] = {
    "claude-sonnet-4-20250514": 15.0 / 1_000_000,
    "claude-haiku-4-5-20251001": 4.0 / 1_000_000,
}


class AnthropicProvider:
    """Anthropic Claude provider."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError("ANTHROPIC_API_KEY is not set and no api_key was provided")
        self._model = model
        self._client = anthropic.Anthropic(api_key=resolved_key)

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, prompt: str, context: dict[str, Any], max_tokens: int) -> Completion:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if hasattr(block, "text"))
        return Completion(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            stop_reason=response.stop_reason or "unknown",
        )

    def estimate_tokens(self, prompt: str, context: dict[str, Any]) -> int:
        input_estimate = len(prompt) // 4
        output_estimate = 1024 // 2  # conservative default ceiling / 2
        return input_estimate + output_estimate

    def cost_per_token(self, direction: Literal["input", "output"]) -> float:
        table = _INPUT_COST if direction == "input" else _OUTPUT_COST
        if self._model not in table:
            raise ValueError(f"No cost data for model {self._model!r}")
        return table[self._model]
