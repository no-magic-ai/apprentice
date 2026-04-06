"""OpenAI provider — GPT API integration."""

from __future__ import annotations

import os
from typing import Any, Literal

import openai

from apprentice.providers.base import Completion

# USD cost per token by model
_INPUT_COST: dict[str, float] = {
    "gpt-4.1": 2.0 / 1_000_000,
    "gpt-4.1-mini": 0.40 / 1_000_000,
}
_OUTPUT_COST: dict[str, float] = {
    "gpt-4.1": 8.0 / 1_000_000,
    "gpt-4.1-mini": 1.60 / 1_000_000,
}


class OpenAIProvider:
    """OpenAI GPT provider."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError("OPENAI_API_KEY is not set and no api_key was provided")
        self._model = model
        self._client = openai.OpenAI(api_key=resolved_key)

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, prompt: str, context: dict[str, Any], max_tokens: int) -> Completion:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
        )
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = response.usage
        if usage is None:
            raise RuntimeError("OpenAI response missing usage data")
        return Completion(
            text=text,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            model=response.model,
            stop_reason=choice.finish_reason or "unknown",
        )

    def estimate_tokens(self, prompt: str, context: dict[str, Any]) -> int:
        input_estimate = len(prompt) // 4
        output_estimate = 1024 // 2
        return input_estimate + output_estimate

    def cost_per_token(self, direction: Literal["input", "output"]) -> float:
        table = _INPUT_COST if direction == "input" else _OUTPUT_COST
        if self._model not in table:
            raise ValueError(f"No cost data for model {self._model!r}")
        return table[self._model]
