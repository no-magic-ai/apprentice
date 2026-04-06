"""OrchestratorAgent — coordinates agents to produce a complete algorithm entry."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from apprentice.core.observability import get_logger, log_stage_metrics
from apprentice.models.artifact import ArtifactBundle
from apprentice.models.work_item import WorkItem, WorkItemStatus

if TYPE_CHECKING:
    from apprentice.agents.base import AgentInterface
    from apprentice.models.agent import AgentContext, AgentResult, AgentTask

_FALLBACK_PCT = 10
_IMPLEMENTATION_PCT = 40
_TOOL_AGENT_PCT = 15
_REVIEW_PCT = 15


@dataclass
class BudgetAllocation:
    """Per-agent token budget, derived from work item total."""

    total_tokens: int
    total_usd: float
    implementation_pct: int = _IMPLEMENTATION_PCT
    tool_agent_pct: int = _TOOL_AGENT_PCT
    review_pct: int = _REVIEW_PCT

    def for_agent(self, agent_type: str) -> tuple[int, float]:
        """Return (tokens, usd) allocation for an agent type.

        Args:
            agent_type: One of "implementation", "instrumentation", or a tool-agent
                        name. Unrecognised types receive a 10% fallback.

        Returns:
            A (tokens, usd) tuple derived from the percentage of the total budget.
        """
        pct_map: dict[str, int] = {
            "implementation": self.implementation_pct,
            "instrumentation": self.tool_agent_pct,
            "visualization": self.tool_agent_pct,
            "anki": self.tool_agent_pct,
            "review": self.review_pct,
        }
        pct = pct_map.get(agent_type, _FALLBACK_PCT)
        tokens = int(self.total_tokens * pct / 100)
        usd = self.total_usd * pct / 100
        return tokens, usd


@dataclass
class OrchestrationResult:
    """Complete result of an orchestrated build."""

    work_item: WorkItem
    artifacts: ArtifactBundle
    agent_results: list[AgentResult] = field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    success: bool = False


class OrchestratorAgent:
    """Coordinates agents to produce a complete algorithm entry."""

    def __init__(
        self,
        agents: dict[str, AgentInterface],
        provider: Any,
    ) -> None:
        self._agents = agents
        self._provider = provider
        self._logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_task(
        self,
        task_type: str,
        work_item: WorkItem,
        input_artifacts: dict[str, str],
        constraints: dict[str, Any],
    ) -> AgentTask:
        from apprentice.models.agent import AgentTask

        return AgentTask(
            task_id=str(uuid.uuid4()),
            task_type=task_type,
            work_item=work_item,
            input_artifacts=input_artifacts,
            constraints=constraints,
        )

    def _make_context(
        self,
        tokens: int,
        usd: float,
        config: dict[str, Any],
        prompt_registry_path: str = "",
    ) -> AgentContext:
        from apprentice.models.agent import AgentContext

        return AgentContext(
            provider=self._provider,
            budget_remaining_tokens=tokens,
            budget_remaining_usd=usd,
            config=config,
            prompt_registry_path=prompt_registry_path,
        )

    def _dispatch(
        self,
        agent_name: str,
        task: AgentTask,
        context: AgentContext,
    ) -> AgentResult | None:
        """Execute one agent; return AgentResult or None on exception."""
        agent = self._agents.get(agent_name)
        if agent is None:
            self._logger.error(
                "agent_not_registered",
                extra={"agent_name": agent_name, "task_id": task.task_id},
            )
            return None

        start = time.monotonic()
        try:
            result: AgentResult = agent.execute(task, context)
        except Exception:
            duration = time.monotonic() - start
            self._logger.exception(
                "agent_exception",
                extra={
                    "agent_name": agent_name,
                    "task_id": task.task_id,
                    "work_item_id": task.work_item.id,
                    "duration_seconds": duration,
                },
            )
            log_stage_metrics(
                stage_name=agent_name,
                tokens_used=0,
                cost_usd=0.0,
                duration_seconds=duration,
                passed=False,
            )
            return None

        duration = time.monotonic() - start
        log_stage_metrics(
            stage_name=agent_name,
            tokens_used=result.tokens_used,
            cost_usd=result.cost_usd,
            duration_seconds=duration,
            passed=result.success,
        )
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def orchestrate(
        self,
        work_item: WorkItem,
        budget: BudgetAllocation,
    ) -> OrchestrationResult:
        """Coordinate agents to produce a complete algorithm entry.

        Current scope (v0.3): implementation agent only.
        Tool-agents and review agent are added in v0.4.

        Args:
            work_item: The algorithm to process.
            budget: Token and USD allocation for this work item.

        Returns:
            OrchestrationResult with artifacts and per-agent results.
        """
        work_item.status = WorkItemStatus.IN_PROGRESS

        bundle = ArtifactBundle(
            id=str(uuid.uuid4()),
            work_item_id=work_item.id,
        )
        agent_results: list[AgentResult] = []
        total_tokens = 0
        total_cost_usd = 0.0

        # --- Implementation agent (40% of budget) ---
        impl_tokens, impl_usd = budget.for_agent("implementation")
        impl_task = self._make_task(
            task_type="implement",
            work_item=work_item,
            input_artifacts={},
            constraints={
                "budget_tokens": impl_tokens,
                "budget_usd": impl_usd,
            },
        )
        impl_context = self._make_context(
            tokens=impl_tokens,
            usd=impl_usd,
            config={},
        )

        self._logger.info(
            "dispatching_agent",
            extra={
                "agent_name": "implementation",
                "task_id": impl_task.task_id,
                "work_item_id": work_item.id,
                "budget_tokens": impl_tokens,
                "budget_usd": impl_usd,
            },
        )

        impl_result = self._dispatch("implementation", impl_task, impl_context)

        if impl_result is None:
            # Exception already logged in _dispatch; shelve and return early.
            work_item.status = WorkItemStatus.SHELVED
            return OrchestrationResult(
                work_item=work_item,
                artifacts=bundle,
                agent_results=agent_results,
                total_tokens=total_tokens,
                total_cost_usd=total_cost_usd,
                success=False,
            )

        agent_results.append(impl_result)
        total_tokens += impl_result.tokens_used
        total_cost_usd += impl_result.cost_usd
        work_item.actual_tokens += impl_result.tokens_used

        if not impl_result.success:
            self._logger.error(
                "agent_reported_failure",
                extra={
                    "agent_name": impl_result.agent_name,
                    "task_id": impl_result.task_id,
                    "work_item_id": work_item.id,
                    "diagnostics": impl_result.diagnostics,
                },
            )
            work_item.status = WorkItemStatus.SHELVED
            return OrchestrationResult(
                work_item=work_item,
                artifacts=bundle,
                agent_results=agent_results,
                total_tokens=total_tokens,
                total_cost_usd=total_cost_usd,
                success=False,
            )

        bundle.implementation_path = impl_result.artifacts.get("implementation", "")

        self._logger.info(
            "agent_completed",
            extra={
                "agent_name": impl_result.agent_name,
                "task_id": impl_result.task_id,
                "work_item_id": work_item.id,
                "tokens_used": impl_result.tokens_used,
                "cost_usd": impl_result.cost_usd,
                "implementation_path": bundle.implementation_path,
            },
        )

        # Tool-agents (instrumentation, visualization, anki) and review agent
        # are added in v0.4.

        work_item.status = WorkItemStatus.COMPLETED
        return OrchestrationResult(
            work_item=work_item,
            artifacts=bundle,
            agent_results=agent_results,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            success=True,
        )
