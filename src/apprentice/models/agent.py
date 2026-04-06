"""AgentTask, AgentResult, and AgentContext models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self

from apprentice.models.work_item import WorkItem


@dataclass
class AgentTask:
    """Input to an agent execution."""

    task_id: str
    task_type: str  # "implement", "instrument", "visualize", etc.
    work_item: WorkItem
    input_artifacts: dict[str, str]  # artifact_type -> file path from prior agents
    constraints: dict[str, Any]  # budget, max_retries, etc.

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "work_item": self.work_item.to_dict(),
            "input_artifacts": self.input_artifacts,
            "constraints": self.constraints,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            task_id=data["task_id"],
            task_type=data["task_type"],
            work_item=WorkItem.from_dict(data["work_item"]),
            input_artifacts=data.get("input_artifacts", {}),
            constraints=data.get("constraints", {}),
        )


@dataclass
class AgentResult:
    """Output of an agent execution."""

    agent_name: str
    task_id: str
    success: bool
    artifacts: dict[str, str]  # artifact_type -> output file path
    tokens_used: int
    cost_usd: float
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    attempt_number: int = 1
    retry_requested: bool = False
    retry_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "task_id": self.task_id,
            "success": self.success,
            "artifacts": self.artifacts,
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "diagnostics": self.diagnostics,
            "attempt_number": self.attempt_number,
            "retry_requested": self.retry_requested,
            "retry_reason": self.retry_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            agent_name=data["agent_name"],
            task_id=data["task_id"],
            success=data["success"],
            artifacts=data["artifacts"],
            tokens_used=data["tokens_used"],
            cost_usd=data["cost_usd"],
            diagnostics=data.get("diagnostics", []),
            attempt_number=data.get("attempt_number", 1),
            retry_requested=data.get("retry_requested", False),
            retry_reason=data.get("retry_reason", ""),
        )


@dataclass
class AgentContext:
    """Carries provider and runtime configuration into an agent execution."""

    provider: Any  # ProviderInterface instance — Any to avoid circular runtime import
    budget_remaining_tokens: int
    budget_remaining_usd: float
    config: dict[str, Any]  # Runtime configuration
    prompt_registry_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_remaining_tokens": self.budget_remaining_tokens,
            "budget_remaining_usd": self.budget_remaining_usd,
            "config": self.config,
            "prompt_registry_path": self.prompt_registry_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], provider: Any) -> Self:
        return cls(
            provider=provider,
            budget_remaining_tokens=data.get("budget_remaining_tokens", 0),
            budget_remaining_usd=data.get("budget_remaining_usd", 0.0),
            config=data.get("config", {}),
            prompt_registry_path=data.get("prompt_registry_path", ""),
        )
