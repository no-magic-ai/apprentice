"""Metrics aggregation — compute success rates, costs, and per-agent breakdowns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.core.session_store import RunRecord


@dataclass
class AgentMetrics:
    """Aggregated metrics for a single agent across multiple runs."""

    agent_name: str
    total_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0

    @property
    def avg_tokens_per_call(self) -> float:
        return self.total_tokens / self.total_calls if self.total_calls > 0 else 0.0

    @property
    def avg_cost_per_call(self) -> float:
        return self.total_cost_usd / self.total_calls if self.total_calls > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "total_calls": self.total_calls,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "avg_tokens_per_call": round(self.avg_tokens_per_call, 1),
            "avg_cost_per_call": round(self.avg_cost_per_call, 6),
        }


@dataclass
class PipelineReport:
    """Aggregated report across multiple pipeline runs."""

    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_duration_seconds: float = 0.0
    per_agent: dict[str, AgentMetrics] = field(default_factory=dict)
    per_tier: dict[int, dict[str, int]] = field(default_factory=dict)
    algorithms: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successful_runs / self.total_runs if self.total_runs > 0 else 0.0

    @property
    def avg_cost_per_algorithm(self) -> float:
        return self.total_cost_usd / self.total_runs if self.total_runs > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "success_rate": round(self.success_rate, 4),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_tokens": self.total_tokens,
            "avg_cost_per_algorithm": round(self.avg_cost_per_algorithm, 6),
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "per_agent": {k: v.to_dict() for k, v in self.per_agent.items()},
            "per_tier": self.per_tier,
            "algorithms": self.algorithms,
        }


def aggregate_runs(records: list[RunRecord]) -> PipelineReport:
    """Aggregate metrics from a list of run records into a report.

    Args:
        records: List of RunRecord instances to aggregate.

    Returns:
        PipelineReport with totals, per-agent, and per-tier breakdowns.
    """
    report = PipelineReport()

    for record in records:
        report.total_runs += 1
        report.total_duration_seconds += record.elapsed_seconds

        if record.status == "completed":
            report.successful_runs += 1
        else:
            report.failed_runs += 1

        tier_entry = report.per_tier.setdefault(record.tier, {"total": 0, "success": 0, "fail": 0})
        tier_entry["total"] += 1
        if record.status == "completed":
            tier_entry["success"] += 1
        else:
            tier_entry["fail"] += 1

        budget = record.budget_summary
        if budget:
            per_agent_data: dict[str, Any] = budget.get("per_agent", {})
            for agent_name, agent_data in per_agent_data.items():
                if agent_name not in report.per_agent:
                    report.per_agent[agent_name] = AgentMetrics(agent_name=agent_name)
                metrics = report.per_agent[agent_name]
                tokens = agent_data.get("tokens_used", 0)
                cost = agent_data.get("cost_usd", 0.0)
                calls = agent_data.get("calls", 0)
                duration = agent_data.get("duration_seconds", 0.0)
                metrics.total_tokens += tokens
                metrics.total_cost_usd += cost
                metrics.total_calls += calls
                metrics.total_duration_seconds += duration
                report.total_tokens += tokens
                report.total_cost_usd += cost

        report.algorithms.append(
            {
                "run_id": record.run_id,
                "algorithm": record.algorithm_name,
                "tier": record.tier,
                "status": record.status,
                "elapsed_seconds": record.elapsed_seconds,
                "error": record.error,
            }
        )

    return report
