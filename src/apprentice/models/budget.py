"""Budget and cost estimation models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self


@dataclass
class CostEstimate:
    """Pre-execution cost estimate for a stage."""

    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            estimated_input_tokens=data["estimated_input_tokens"],
            estimated_output_tokens=data["estimated_output_tokens"],
            estimated_cost_usd=data["estimated_cost_usd"],
        )


@dataclass
class BudgetLogEntry:
    """Recorded cost of a stage execution."""

    id: str
    cycle_id: str
    work_item_id: str
    stage_name: str
    provider: str
    model: str
    estimated_tokens: int
    actual_tokens: int
    estimated_cost_usd: float
    actual_cost_usd: float
    logged_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cycle_id": self.cycle_id,
            "work_item_id": self.work_item_id,
            "stage_name": self.stage_name,
            "provider": self.provider,
            "model": self.model,
            "estimated_tokens": self.estimated_tokens,
            "actual_tokens": self.actual_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "actual_cost_usd": self.actual_cost_usd,
            "logged_at": self.logged_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            id=data["id"],
            cycle_id=data["cycle_id"],
            work_item_id=data["work_item_id"],
            stage_name=data["stage_name"],
            provider=data["provider"],
            model=data["model"],
            estimated_tokens=data["estimated_tokens"],
            actual_tokens=data["actual_tokens"],
            estimated_cost_usd=data["estimated_cost_usd"],
            actual_cost_usd=data["actual_cost_usd"],
            logged_at=datetime.fromisoformat(data["logged_at"]),
        )
