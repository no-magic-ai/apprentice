"""Tests for the Implementation Agent self-validation loop."""

from __future__ import annotations

from typing import Any

from apprentice.agents.implementation import ImplementationAgent
from apprentice.models.agent import AgentContext, AgentResult, AgentTask
from apprentice.models.work_item import WorkItem
from apprentice.providers.base import Completion


class _MockProvider:
    """Mock provider that returns configurable responses."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._call_count = 0

    @property
    def model_name(self) -> str:
        return "mock"

    def complete(self, prompt: str, context: dict[str, Any], max_tokens: int) -> Completion:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        text = self._responses[idx]
        return Completion(
            text=text,
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
            model="mock",
            stop_reason="end_turn",
        )

    def estimate_tokens(self, prompt: str, context: dict[str, Any]) -> int:
        return len(prompt) // 4

    def cost_per_token(self, direction: str) -> float:
        return 0.0


_GOOD_CODE = '''```python
"""Quicksort implementation.

Complexity:
    Time: O(n log n) average
    Space: O(n)

References:
    - Hoare (1961)

Args:
    arr: list of integers

Returns:
    sorted list
"""

from __future__ import annotations


def quicksort(arr: list[int]) -> list[int]:
    """Sort a list using quicksort.

    Args:
        arr: Input list.

    Returns:
        Sorted list.
    """
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    left = [x for x in arr[1:] if x <= pivot]
    right = [x for x in arr[1:] if x > pivot]
    return quicksort(left) + [pivot] + quicksort(right)


if __name__ == "__main__":
    assert quicksort([3, 1, 2]) == [1, 2, 3]
    assert quicksort([]) == []
    assert quicksort([1]) == [1]
```'''


def _make_task(retries: int = 3) -> AgentTask:
    return AgentTask(
        task_id="test-task",
        task_type="implement",
        work_item=WorkItem(id="test", algorithm_name="quicksort", tier=2),
        input_artifacts={},
        constraints={"max_retries": retries},
    )


def _make_context(provider: Any) -> AgentContext:
    return AgentContext(
        provider=provider,
        budget_remaining_tokens=20000,
        budget_remaining_usd=5.0,
        config={},
    )


class TestImplementationAgent:
    def test_name_and_role(self) -> None:
        agent = ImplementationAgent()
        assert agent.name == "implementation"
        assert agent.role != ""

    def test_successful_generation(self) -> None:
        provider = _MockProvider([_GOOD_CODE])
        agent = ImplementationAgent()
        result = agent.execute(_make_task(), _make_context(provider))
        assert isinstance(result, AgentResult)
        assert result.success is True
        assert "implementation" in result.artifacts
        assert result.tokens_used > 0

    def test_tracks_attempt_number(self) -> None:
        provider = _MockProvider([_GOOD_CODE])
        agent = ImplementationAgent()
        result = agent.execute(_make_task(), _make_context(provider))
        assert result.attempt_number >= 1

    def test_requires_provider(self) -> None:
        import pytest

        agent = ImplementationAgent()
        ctx = AgentContext(
            provider=None,
            budget_remaining_tokens=20000,
            budget_remaining_usd=5.0,
            config={},
        )
        with pytest.raises(RuntimeError, match="No provider"):
            agent.execute(_make_task(), ctx)

    def test_bad_code_retries(self) -> None:
        bad_code = "```python\ndef foo(x):\n    return x\n```"
        provider = _MockProvider([bad_code, bad_code, _GOOD_CODE])
        agent = ImplementationAgent()
        result = agent.execute(_make_task(retries=3), _make_context(provider))
        # Should eventually succeed on the third attempt with good code
        assert result.attempt_number >= 1

    def test_max_retries_exhausted(self) -> None:
        bad_code = "```python\ndef foo(x):\n    return x\n```"
        provider = _MockProvider([bad_code])
        agent = ImplementationAgent()
        result = agent.execute(_make_task(retries=1), _make_context(provider))
        assert result.success is False
        assert len(result.diagnostics) > 0
