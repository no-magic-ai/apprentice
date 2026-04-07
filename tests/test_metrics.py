"""Tests for metrics aggregation."""

from __future__ import annotations

from apprentice.core.metrics import AgentMetrics, PipelineReport, aggregate_runs
from apprentice.core.session_store import RunRecord


def _make_record(
    name: str = "quicksort",
    tier: int = 2,
    status: str = "completed",
    elapsed: float = 10.0,
    error: str = "",
    budget: dict | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=f"{name}-test",
        algorithm_name=name,
        tier=tier,
        status=status,
        session_state={"generated_code": "code"} if status == "completed" else {},
        budget_summary=budget or {},
        started_at="2026-01-01T00:00:00",
        completed_at="2026-01-01T00:01:00",
        elapsed_seconds=elapsed,
        error=error,
    )


class TestAgentMetrics:
    def test_avg_tokens_per_call(self) -> None:
        m = AgentMetrics(agent_name="test", total_calls=10, total_tokens=1000)
        assert m.avg_tokens_per_call == 100.0

    def test_avg_tokens_zero_calls(self) -> None:
        m = AgentMetrics(agent_name="test")
        assert m.avg_tokens_per_call == 0.0

    def test_to_dict(self) -> None:
        m = AgentMetrics(agent_name="test", total_calls=5, total_tokens=500, total_cost_usd=0.05)
        d = m.to_dict()
        assert d["agent_name"] == "test"
        assert d["avg_tokens_per_call"] == 100.0


class TestPipelineReport:
    def test_success_rate_all_pass(self) -> None:
        report = PipelineReport(total_runs=10, successful_runs=10)
        assert report.success_rate == 1.0

    def test_success_rate_partial(self) -> None:
        report = PipelineReport(total_runs=10, successful_runs=8, failed_runs=2)
        assert report.success_rate == 0.8

    def test_success_rate_zero_runs(self) -> None:
        report = PipelineReport()
        assert report.success_rate == 0.0

    def test_avg_cost(self) -> None:
        report = PipelineReport(total_runs=5, total_cost_usd=10.0)
        assert report.avg_cost_per_algorithm == 2.0


class TestAggregateRuns:
    def test_empty(self) -> None:
        report = aggregate_runs([])
        assert report.total_runs == 0
        assert report.success_rate == 0.0

    def test_single_completed(self) -> None:
        records = [_make_record(status="completed", elapsed=10.0)]
        report = aggregate_runs(records)
        assert report.total_runs == 1
        assert report.successful_runs == 1
        assert report.failed_runs == 0
        assert report.success_rate == 1.0

    def test_mixed_statuses(self) -> None:
        records = [
            _make_record("algo1", status="completed"),
            _make_record("algo2", status="failed", error="boom"),
            _make_record("algo3", status="completed"),
        ]
        report = aggregate_runs(records)
        assert report.total_runs == 3
        assert report.successful_runs == 2
        assert report.failed_runs == 1

    def test_per_tier_breakdown(self) -> None:
        records = [
            _make_record("algo1", tier=1, status="completed"),
            _make_record("algo2", tier=1, status="failed"),
            _make_record("algo3", tier=2, status="completed"),
        ]
        report = aggregate_runs(records)
        assert report.per_tier[1]["total"] == 2
        assert report.per_tier[1]["success"] == 1
        assert report.per_tier[2]["total"] == 1

    def test_per_agent_budget(self) -> None:
        budget = {
            "per_agent": {
                "drafter": {
                    "tokens_used": 1000,
                    "cost_usd": 0.01,
                    "calls": 1,
                    "duration_seconds": 5.0,
                },
                "self_reviewer": {
                    "tokens_used": 500,
                    "cost_usd": 0.005,
                    "calls": 1,
                    "duration_seconds": 3.0,
                },
            }
        }
        records = [_make_record(budget=budget)]
        report = aggregate_runs(records)
        assert "drafter" in report.per_agent
        assert report.per_agent["drafter"].total_tokens == 1000
        assert report.total_tokens == 1500

    def test_to_dict(self) -> None:
        records = [_make_record()]
        report = aggregate_runs(records)
        d = report.to_dict()
        assert "total_runs" in d
        assert "success_rate" in d
        assert "per_agent" in d
        assert "algorithms" in d

    def test_algorithms_list(self) -> None:
        records = [
            _make_record("algo1", status="completed"),
            _make_record("algo2", status="failed", error="err"),
        ]
        report = aggregate_runs(records)
        assert len(report.algorithms) == 2
        assert report.algorithms[0]["algorithm"] == "algo1"
        assert report.algorithms[1]["error"] == "err"
