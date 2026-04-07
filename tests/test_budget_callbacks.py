"""Tests for ADK budget tracking and callbacks."""

from __future__ import annotations

import asyncio

from apprentice.core.budget import (
    BudgetTracker,
    make_after_agent_callback,
    make_after_model_callback,
    make_before_agent_callback,
    make_before_model_callback,
)


class TestBudgetTracker:
    def test_initial_state(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        assert tracker.tokens_remaining == 100_000
        assert tracker.usd_remaining == 5.0
        assert tracker.is_exhausted is False

    def test_record_agent_completion(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        tracker.record_agent_start("test_agent")
        tracker.record_agent_completion("test_agent", tokens=1000, cost=0.01)
        assert tracker.tokens_used == 1000
        assert tracker.cost_usd == 0.01
        assert tracker.tokens_remaining == 99_000

    def test_is_exhausted_tokens(self) -> None:
        tracker = BudgetTracker(total_tokens=100, total_usd=5.0)
        tracker.record_agent_completion("a", tokens=100, cost=0.0)
        assert tracker.is_exhausted is True

    def test_is_exhausted_cost(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=0.01)
        tracker.record_agent_completion("a", tokens=0, cost=0.01)
        assert tracker.is_exhausted is True

    def test_per_agent_tracking(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        tracker.record_agent_start("agent_a")
        tracker.record_agent_completion("agent_a", tokens=500, cost=0.01)
        tracker.record_agent_start("agent_a")
        tracker.record_agent_completion("agent_a", tokens=300, cost=0.005)

        assert tracker.per_agent["agent_a"]["tokens_used"] == 800
        assert tracker.per_agent["agent_a"]["calls"] == 2

    def test_to_dict(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        tracker.record_agent_start("test")
        tracker.record_agent_completion("test", tokens=100, cost=0.001)
        d = tracker.to_dict()
        assert d["total_tokens"] == 100_000
        assert d["tokens_used"] == 100
        assert "per_agent" in d


class TestCallbackFactories:
    def test_before_agent_callback_returns_none(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        cb = make_before_agent_callback(tracker)

        class MockContext:
            agent_name = "test"

        result = asyncio.run(cb(MockContext()))
        assert result is None

    def test_after_agent_callback_returns_none(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        cb = make_after_agent_callback(tracker)

        class MockContext:
            agent_name = "test"

        result = asyncio.run(cb(MockContext()))
        assert result is None

    def test_before_model_callback_returns_none(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        cb = make_before_model_callback(tracker)

        result = asyncio.run(cb(None, None))
        assert result is None

    def test_after_model_callback_returns_none(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        cb = make_after_model_callback(tracker)

        result = asyncio.run(cb(None, None))
        assert result is None
