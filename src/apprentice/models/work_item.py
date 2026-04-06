"""WorkItem, StageResult, GateResult, and PipelineContext models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Self


class WorkItemStatus(StrEnum):
    """Valid states for a work item per §10.2 state machine."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REVISION_REQUESTED = "revision_requested"
    SHELVED = "shelved"
    ARCHIVED = "archived"


class WorkItemSource(StrEnum):
    """Origin of a work item."""

    DISCOVERY = "discovery"
    MANUAL = "manual"
    GITHUB_ISSUE = "github_issue"


class GateVerdict(StrEnum):
    """Gate evaluation outcomes."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass
class WorkItem:
    """A single algorithm to be processed through the pipeline."""

    id: str
    algorithm_name: str
    tier: int
    status: WorkItemStatus = WorkItemStatus.QUEUED
    source: WorkItemSource = WorkItemSource.MANUAL
    rationale: str = ""
    allocated_tokens: int = 0
    actual_tokens: int = 0
    last_failed_stage: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "algorithm_name": self.algorithm_name,
            "tier": self.tier,
            "status": self.status.value,
            "source": self.source.value,
            "rationale": self.rationale,
            "allocated_tokens": self.allocated_tokens,
            "actual_tokens": self.actual_tokens,
            "last_failed_stage": self.last_failed_stage,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat()
            if self.completed_at is not None
            else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            id=data["id"],
            algorithm_name=data["algorithm_name"],
            tier=data["tier"],
            status=WorkItemStatus(data["status"]),
            source=WorkItemSource(data["source"]),
            rationale=data.get("rationale", ""),
            allocated_tokens=data.get("allocated_tokens", 0),
            actual_tokens=data.get("actual_tokens", 0),
            last_failed_stage=data.get("last_failed_stage"),
            created_at=datetime.fromisoformat(data["created_at"]),
            completed_at=datetime.fromisoformat(data["completed_at"])
            if data.get("completed_at") is not None
            else None,
        )


@dataclass
class StageResult:
    """Output of a pipeline stage execution."""

    stage_name: str
    artifacts: dict[str, str]  # artifact_type -> relative file path
    tokens_used: int
    cost_usd: float
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "artifacts": self.artifacts,
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            stage_name=data["stage_name"],
            artifacts=data["artifacts"],
            tokens_used=data["tokens_used"],
            cost_usd=data["cost_usd"],
            diagnostics=data.get("diagnostics", []),
        )


@dataclass
class GateResult:
    """Output of a quality gate evaluation."""

    gate_name: str
    verdict: GateVerdict
    diagnostics: dict[str, Any] = field(default_factory=dict)
    auto_fixable: bool = False
    fix_suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_name": self.gate_name,
            "verdict": self.verdict.value,
            "diagnostics": self.diagnostics,
            "auto_fixable": self.auto_fixable,
            "fix_suggestion": self.fix_suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            gate_name=data["gate_name"],
            verdict=GateVerdict(data["verdict"]),
            diagnostics=data.get("diagnostics", {}),
            auto_fixable=data.get("auto_fixable", False),
            fix_suggestion=data.get("fix_suggestion"),
        )


@dataclass
class PipelineContext:
    """Carries configuration and state through the pipeline."""

    config: dict[str, Any] = field(default_factory=dict)
    budget_remaining_tokens: int = 0
    budget_remaining_usd: float = 0.0
    prompt_registry_path: str = ""
    convention_schema_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "budget_remaining_tokens": self.budget_remaining_tokens,
            "budget_remaining_usd": self.budget_remaining_usd,
            "prompt_registry_path": self.prompt_registry_path,
            "convention_schema_path": self.convention_schema_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            config=data.get("config", {}),
            budget_remaining_tokens=data.get("budget_remaining_tokens", 0),
            budget_remaining_usd=data.get("budget_remaining_usd", 0.0),
            prompt_registry_path=data.get("prompt_registry_path", ""),
            convention_schema_path=data.get("convention_schema_path", ""),
        )
