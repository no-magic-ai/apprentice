"""Model factory — creates ADK-compatible model instances from config."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from google.adk.models.lite_llm import LiteLlm

if TYPE_CHECKING:
    from apprentice.core.config import ProviderConfig

# Backend → required env var (None means no env var needed)
_REQUIRED_ENV_VARS: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "ollama": None,
    "local": None,
    "claude_cli": None,
}

_SUPPORTED_BACKENDS = frozenset(_REQUIRED_ENV_VARS.keys())


def create_model(config: ProviderConfig) -> Any:
    """Create an ADK-compatible model instance from apprentice config.

    Supports backends: anthropic, openai, gemini, ollama, local, claude_cli.

    For ollama: sets OLLAMA_API_BASE from config.local_api_base.
    For local: sets OPENAI_API_BASE and OPENAI_API_KEY=not-needed from config.
    For claude_cli: uses `claude -p` subprocess (no API key needed).
    For cloud providers: reads API keys from environment variables.

    Args:
        config: Provider configuration from apprentice.toml.

    Returns:
        A LiteLlm or ClaudeCli instance configured for the specified backend.

    Raises:
        ValueError: If the backend is not supported.
        RuntimeError: If a required API key environment variable is missing.
    """
    backend = config.backend
    if backend not in _SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unsupported backend {backend!r}. Supported: {sorted(_SUPPORTED_BACKENDS)}"
        )

    if backend == "claude_cli":
        from apprentice.providers.claude_cli import ClaudeCli

        return ClaudeCli(model=config.model or "claude-cli")

    _configure_environment(backend, config.local_api_base)

    return LiteLlm(model=config.model)


def create_model_from_override(
    model_string: str,
    backend: str,
    local_api_base: str = "",
) -> Any:
    """Create an ADK model from CLI override flags.

    Args:
        model_string: LiteLlm model string (e.g. "ollama_chat/llama3.3").
        backend: Backend identifier for environment setup.
        local_api_base: API base URL for ollama/local backends.

    Returns:
        A LiteLlm or ClaudeCli instance.

    Raises:
        ValueError: If the backend is not supported.
        RuntimeError: If a required API key is missing.
    """
    if backend not in _SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unsupported backend {backend!r}. Supported: {sorted(_SUPPORTED_BACKENDS)}"
        )

    if backend == "claude_cli":
        from apprentice.providers.claude_cli import ClaudeCli

        return ClaudeCli(model=model_string or "claude-cli")

    _configure_environment(backend, local_api_base)

    return LiteLlm(model=model_string)


def _configure_environment(backend: str, local_api_base: str) -> None:
    """Set environment variables required by the backend.

    Args:
        backend: Backend identifier.
        local_api_base: API base URL for local/ollama backends.

    Raises:
        RuntimeError: If a required API key is not set.
    """
    if backend == "ollama":
        if local_api_base:
            os.environ["OLLAMA_API_BASE"] = local_api_base
    elif backend == "local":
        if local_api_base:
            os.environ["OPENAI_API_BASE"] = local_api_base
        os.environ.setdefault("OPENAI_API_KEY", "not-needed")
    else:
        env_var = _REQUIRED_ENV_VARS.get(backend)
        if env_var and not os.environ.get(env_var):
            raise RuntimeError(
                f"Backend {backend!r} requires environment variable {env_var} to be set"
            )
