"""Tests for the OrchestratorAgent."""

from __future__ import annotations

from typing import Any, ClassVar

from apprentice.core.orchestrator import BudgetAllocation, OrchestrationResult, OrchestratorAgent
from apprentice.models.agent import AgentContext, AgentResult, AgentTask
from apprentice.models.work_item import WorkItem, WorkItemStatus


class _StubAgent:
    """Minimal agent for testing orchestrator dispatch."""

    name = "implementation"
    role = "stub"
    system_prompt = "stub"
    allowed_tools: ClassVar[list[str]] = []

    def __init__(self, success: bool = True, tokens: int = 100) -> None:
        self._success = success
        self._tokens = tokens

    def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        return AgentResult(
            agent_name=self.name,
            task_id=task.task_id,
            success=self._success,
            artifacts={"implementation": "/tmp/stub.py"} if self._success else {},
            tokens_used=self._tokens,
            cost_usd=0.001,
            diagnostics=[],
        )


class _FailingAgent(_StubAgent):
    def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        raise RuntimeError("agent explosion")


def _make_budget(tokens: int = 100_000) -> BudgetAllocation:
    return BudgetAllocation(total_tokens=tokens, total_usd=5.0)


class TestBudgetAllocation:
    def test_implementation_gets_40_pct(self) -> None:
        budget = _make_budget(100_000)
        tokens, usd = budget.for_agent("implementation")
        assert tokens == 40_000
        assert usd == 2.0

    def test_tool_agent_gets_15_pct(self) -> None:
        budget = _make_budget(100_000)
        tokens, _ = budget.for_agent("instrumentation")
        assert tokens == 15_000

    def test_unknown_gets_fallback(self) -> None:
        budget = _make_budget(100_000)
        tokens, _ = budget.for_agent("unknown")
        assert tokens == 10_000


class TestOrchestratorSuccess:
    def test_successful_orchestration(self) -> None:
        agents: dict[str, Any] = {"implementation": _StubAgent(success=True)}
        orch = OrchestratorAgent(agents=agents, provider=None)
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        result = orch.orchestrate(item, _make_budget())
        assert isinstance(result, OrchestrationResult)
        assert result.success is True
        assert item.status == WorkItemStatus.COMPLETED
        assert len(result.agent_results) == 1
        assert result.total_tokens == 100

    def test_artifacts_merged(self) -> None:
        agents: dict[str, Any] = {"implementation": _StubAgent(success=True)}
        orch = OrchestratorAgent(agents=agents, provider=None)
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        result = orch.orchestrate(item, _make_budget())
        assert result.artifacts.implementation_path == "/tmp/stub.py"


class TestOrchestratorFailure:
    def test_agent_failure_shelves(self) -> None:
        agents: dict[str, Any] = {"implementation": _StubAgent(success=False)}
        orch = OrchestratorAgent(agents=agents, provider=None)
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        result = orch.orchestrate(item, _make_budget())
        assert result.success is False
        assert item.status == WorkItemStatus.SHELVED

    def test_agent_exception_shelves(self) -> None:
        agents: dict[str, Any] = {"implementation": _FailingAgent()}
        orch = OrchestratorAgent(agents=agents, provider=None)
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        result = orch.orchestrate(item, _make_budget())
        assert result.success is False
        assert item.status == WorkItemStatus.SHELVED

    def test_missing_agent_shelves(self) -> None:
        orch = OrchestratorAgent(agents={}, provider=None)
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        result = orch.orchestrate(item, _make_budget())
        assert result.success is False
        assert item.status == WorkItemStatus.SHELVED
