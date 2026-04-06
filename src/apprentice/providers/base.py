"""ProviderInterface protocol — contract for LLM providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass
class Completion:
    """Full LLM response with usage metadata."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str


@runtime_checkable
class ProviderInterface(Protocol):
    """Contract for LLM providers.

    Providers handle completion, token estimation, and cost reporting.
    The max_tokens parameter on complete() enforces a hard ceiling —
    the provider kills the response if tokens exceed the remaining stage budget.
    """

    @property
    def model_name(self) -> str:
        """Active model identifier."""
        ...

    def complete(self, prompt: str, context: dict[str, Any], max_tokens: int) -> Completion:
        """Generate completion with hard token ceiling."""
        ...

    def estimate_tokens(self, prompt: str, context: dict[str, Any]) -> int:
        """Estimate total tokens (input + output) for budget pre-checks."""
        ...

    def cost_per_token(self, direction: Literal["input", "output"]) -> float:
        """Return USD cost per token for the active model."""
        ...
