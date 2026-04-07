"""LLM provider abstractions — thin interface over Anthropic, OpenAI, etc."""

from __future__ import annotations

from apprentice.providers.anthropic import AnthropicProvider
from apprentice.providers.base import Completion, ProviderInterface
from apprentice.providers.factory import create_model, create_model_from_override
from apprentice.providers.openai import OpenAIProvider

__all__ = [
    "AnthropicProvider",
    "Completion",
    "OpenAIProvider",
    "ProviderInterface",
    "create_model",
    "create_model_from_override",
    "create_provider",
]


def create_provider(
    provider_name: str,
    model: str,
    api_key: str | None = None,
) -> ProviderInterface:
    """Instantiate the named provider with the given model.

    Args:
        provider_name: "anthropic" or "openai"
        model: Model identifier string (e.g. "claude-sonnet-4-20250514")
        api_key: Optional API key; falls back to the provider's env var.

    Raises:
        ValueError: If provider_name is not recognised.
    """
    if provider_name == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key)
    if provider_name == "openai":
        return OpenAIProvider(model=model, api_key=api_key)
    raise ValueError(f"Unknown provider {provider_name!r}. Supported: 'anthropic', 'openai'.")
