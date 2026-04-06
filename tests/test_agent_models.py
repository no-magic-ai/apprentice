"""Tests for agent data models and serialization."""

from __future__ import annotations

from apprentice.models.agent import AgentContext, AgentResult, AgentTask
from apprentice.models.work_item import WorkItem


class TestAgentTaskSerialization:
    def test_round_trip(self) -> None:
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        task = AgentTask(
            task_id="task-1",
            task_type="implement",
            work_item=item,
            input_artifacts={"implementation": "/tmp/qs.py"},
            constraints={"max_retries": 3},
        )
        data = task.to_dict()
        restored = AgentTask.from_dict(data)
        assert restored.task_id == "task-1"
        assert restored.task_type == "implement"
        assert restored.work_item.algorithm_name == "quicksort"
        assert restored.input_artifacts["implementation"] == "/tmp/qs.py"
        assert restored.constraints["max_retries"] == 3


class TestAgentResultSerialization:
    def test_round_trip(self) -> None:
        result = AgentResult(
            agent_name="implementation",
            task_id="task-1",
            success=True,
            artifacts={"implementation": "/tmp/qs.py"},
            tokens_used=5000,
            cost_usd=0.045,
            diagnostics=[{"check": "lint", "passed": True}],
            attempt_number=2,
            retry_requested=False,
        )
        data = result.to_dict()
        restored = AgentResult.from_dict(data)
        assert restored.agent_name == "implementation"
        assert restored.success is True
        assert restored.attempt_number == 2
        assert restored.tokens_used == 5000

    def test_defaults(self) -> None:
        result = AgentResult(
            agent_name="test",
            task_id="t",
            success=True,
            artifacts={},
            tokens_used=0,
            cost_usd=0.0,
        )
        assert result.attempt_number == 1
        assert result.retry_requested is False
        assert result.retry_reason == ""


class TestAgentContextSerialization:
    def test_round_trip(self) -> None:
        ctx = AgentContext(
            provider="mock",
            budget_remaining_tokens=10000,
            budget_remaining_usd=5.0,
            config={"key": "value"},
        )
        data = ctx.to_dict()
        restored = AgentContext.from_dict(data, provider="mock")
        assert restored.budget_remaining_tokens == 10000
        assert restored.config["key"] == "value"
