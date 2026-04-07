"""Tests for the LiteLlm provider factory."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from apprentice.core.config import ProviderConfig
from apprentice.providers.factory import (
    _configure_environment,
    create_model,
    create_model_from_override,
)


def _make_config(
    backend: str = "anthropic",
    model: str = "anthropic/claude-sonnet-4-20250514",
    fallback: str = "anthropic/claude-haiku-4-5-20251001",
    local_api_base: str = "",
) -> ProviderConfig:
    return ProviderConfig(
        backend=backend,
        model=model,
        fallback_model=fallback,
        local_api_base=local_api_base,
    )


class TestCreateModel:
    def test_anthropic_backend(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(_make_config(backend="anthropic"))
            assert model is not None

    def test_openai_backend(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            model = create_model(_make_config(backend="openai", model="openai/gpt-4.1"))
            assert model is not None

    def test_gemini_backend(self) -> None:
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            model = create_model(_make_config(backend="gemini", model="gemini/gemini-2.5-flash"))
            assert model is not None

    def test_ollama_backend_no_key_required(self) -> None:
        model = create_model(
            _make_config(
                backend="ollama",
                model="ollama_chat/llama3.3",
                local_api_base="http://localhost:11434",
            )
        )
        assert model is not None

    def test_local_backend_sets_env(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            model = create_model(
                _make_config(
                    backend="local",
                    model="openai/local-model",
                    local_api_base="http://localhost:8000",
                )
            )
            assert model is not None
            assert os.environ.get("OPENAI_API_BASE") == "http://localhost:8000"

    def test_unsupported_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported backend"):
            create_model(_make_config(backend="unsupported"))

    def test_missing_api_key_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            # Ensure ANTHROPIC_API_KEY is not set
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
                create_model(_make_config(backend="anthropic"))


class TestCreateModelFromOverride:
    def test_override_model_string(self) -> None:
        model = create_model_from_override(
            model_string="ollama_chat/llama3.3",
            backend="ollama",
            local_api_base="http://localhost:11434",
        )
        assert model is not None

    def test_unsupported_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported backend"):
            create_model_from_override(
                model_string="some/model",
                backend="invalid",
            )


class TestConfigureEnvironment:
    def test_ollama_sets_api_base(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            _configure_environment("ollama", "http://localhost:11434")
            assert os.environ.get("OLLAMA_API_BASE") == "http://localhost:11434"

    def test_local_sets_api_base_and_key(self) -> None:
        env_without_key = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            _configure_environment("local", "http://localhost:8000")
            assert os.environ.get("OPENAI_API_BASE") == "http://localhost:8000"
            assert os.environ.get("OPENAI_API_KEY") == "not-needed"

    def test_ollama_empty_base_no_change(self) -> None:
        original = os.environ.get("OLLAMA_API_BASE")
        _configure_environment("ollama", "")
        assert os.environ.get("OLLAMA_API_BASE") == original
