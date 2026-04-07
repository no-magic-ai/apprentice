"""Tests for the ADK orchestrator pipeline builder (backward compat)."""

from __future__ import annotations

from apprentice.core.budget import BudgetTracker


class TestBudgetTracker:
    def test_implementation_tokens(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        assert tracker.tokens_remaining == 100_000

    def test_deduction(self) -> None:
        tracker = BudgetTracker(total_tokens=100_000, total_usd=5.0)
        tracker.record_agent_start("implementation")
        tracker.record_agent_completion("implementation", tokens=40_000, cost=2.0)
        assert tracker.tokens_remaining == 60_000
        assert tracker.usd_remaining == 3.0

    def test_exhaustion(self) -> None:
        tracker = BudgetTracker(total_tokens=100, total_usd=5.0)
        tracker.record_agent_completion("a", tokens=100, cost=0.0)
        assert tracker.is_exhausted is True
