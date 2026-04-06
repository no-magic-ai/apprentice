"""Cycle model — tracks autonomous execution cycles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self


@dataclass
class Cycle:
    """A single autonomous execution cycle."""

    id: str
    started_at: datetime
    ended_at: datetime | None = None
    items_attempted: int = 0
    items_completed: int = 0
    items_shelved: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    circuit_state: str = "closed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at is not None else None,
            "items_attempted": self.items_attempted,
            "items_completed": self.items_completed,
            "items_shelved": self.items_shelved,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "circuit_state": self.circuit_state,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            id=data["id"],
            started_at=datetime.fromisoformat(data["started_at"]),
            ended_at=datetime.fromisoformat(data["ended_at"])
            if data.get("ended_at") is not None
            else None,
            items_attempted=data.get("items_attempted", 0),
            items_completed=data.get("items_completed", 0),
            items_shelved=data.get("items_shelved", 0),
            total_tokens=data.get("total_tokens", 0),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            circuit_state=data.get("circuit_state", "closed"),
        )
