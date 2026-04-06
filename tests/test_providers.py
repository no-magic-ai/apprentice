"""Tests for provider factory and interface compliance."""

from __future__ import annotations

import pytest

from apprentice.providers import create_provider
from apprentice.providers.base import Completion, ProviderInterface


class TestProviderFactory:
    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("nonexistent", "some-model")

    def test_anthropic_requires_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            create_provider("anthropic", "claude-sonnet-4-20250514")

    def test_openai_requires_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            create_provider("openai", "gpt-4.1")

    def test_anthropic_with_key(self) -> None:
        provider = create_provider("anthropic", "claude-sonnet-4-20250514", api_key="test-key")
        assert isinstance(provider, ProviderInterface)
        assert provider.model_name == "claude-sonnet-4-20250514"

    def test_openai_with_key(self) -> None:
        provider = create_provider("openai", "gpt-4.1", api_key="test-key")
        assert isinstance(provider, ProviderInterface)
        assert provider.model_name == "gpt-4.1"


class TestCompletion:
    def test_fields(self) -> None:
        c = Completion(
            text="hello",
            input_tokens=10,
            output_tokens=5,
            model="test",
            stop_reason="end_turn",
        )
        assert c.text == "hello"
        assert c.input_tokens == 10


class TestCostPerToken:
    def test_anthropic_sonnet_rates(self) -> None:
        provider = create_provider("anthropic", "claude-sonnet-4-20250514", api_key="k")
        input_rate = provider.cost_per_token("input")
        output_rate = provider.cost_per_token("output")
        assert input_rate == pytest.approx(3.0 / 1_000_000)
        assert output_rate == pytest.approx(15.0 / 1_000_000)

    def test_openai_gpt41_rates(self) -> None:
        provider = create_provider("openai", "gpt-4.1", api_key="k")
        input_rate = provider.cost_per_token("input")
        output_rate = provider.cost_per_token("output")
        assert input_rate == pytest.approx(2.0 / 1_000_000)
        assert output_rate == pytest.approx(8.0 / 1_000_000)

    def test_unknown_model_raises(self) -> None:
        provider = create_provider("anthropic", "claude-sonnet-4-20250514", api_key="k")
        # Override the model to something unknown
        provider._model = "unknown-model"  # type: ignore[attr-defined]
        with pytest.raises(ValueError, match="No cost data"):
            provider.cost_per_token("input")


class TestTokenEstimation:
    def test_returns_positive(self) -> None:
        provider = create_provider("anthropic", "claude-sonnet-4-20250514", api_key="k")
        estimate = provider.estimate_tokens("Hello, implement quicksort", {})
        assert estimate > 0
