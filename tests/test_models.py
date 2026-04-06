"""Tests for model serialization round-trips."""

from __future__ import annotations

from datetime import datetime

from apprentice.models.artifact import ArtifactBundle
from apprentice.models.budget import BudgetLogEntry, CostEstimate
from apprentice.models.cycle import Cycle
from apprentice.models.work_item import (
    GateResult,
    GateVerdict,
    PipelineContext,
    StageResult,
    WorkItem,
    WorkItemSource,
    WorkItemStatus,
)


class TestWorkItemSerialization:
    def test_round_trip(self) -> None:
        item = WorkItem(
            id="test-1",
            algorithm_name="quicksort",
            tier=2,
            status=WorkItemStatus.IN_PROGRESS,
            source=WorkItemSource.MANUAL,
            rationale="test rationale",
        )
        data = item.to_dict()
        restored = WorkItem.from_dict(data)
        assert restored.id == item.id
        assert restored.algorithm_name == item.algorithm_name
        assert restored.tier == item.tier
        assert restored.status == WorkItemStatus.IN_PROGRESS
        assert restored.source == WorkItemSource.MANUAL

    def test_enum_serialization(self) -> None:
        item = WorkItem(id="t", algorithm_name="a", tier=1, status=WorkItemStatus.SHELVED)
        data = item.to_dict()
        assert data["status"] == "shelved"

    def test_optional_datetime(self) -> None:
        item = WorkItem(id="t", algorithm_name="a", tier=1)
        data = item.to_dict()
        assert data["completed_at"] is None
        restored = WorkItem.from_dict(data)
        assert restored.completed_at is None


class TestStageResultSerialization:
    def test_round_trip(self) -> None:
        result = StageResult(
            stage_name="implementation",
            artifacts={"implementation": "path/to/file.py"},
            tokens_used=5000,
            cost_usd=0.045,
            diagnostics=[{"level": "warning", "message": "test"}],
        )
        data = result.to_dict()
        restored = StageResult.from_dict(data)
        assert restored.stage_name == result.stage_name
        assert restored.artifacts == result.artifacts
        assert restored.tokens_used == result.tokens_used


class TestGateResultSerialization:
    def test_round_trip(self) -> None:
        result = GateResult(
            gate_name="correctness",
            verdict=GateVerdict.PASS,
            diagnostics={"tests_passed": 3},
        )
        data = result.to_dict()
        restored = GateResult.from_dict(data)
        assert restored.verdict == GateVerdict.PASS
        assert restored.diagnostics["tests_passed"] == 3


class TestArtifactBundleSerialization:
    def test_round_trip(self) -> None:
        bundle = ArtifactBundle(
            id="bundle-1",
            work_item_id="item-1",
            revision_number=2,
            parent_bundle_id="bundle-0",
            template_version="1.0.0",
        )
        data = bundle.to_dict()
        restored = ArtifactBundle.from_dict(data)
        assert restored.revision_number == 2
        assert restored.parent_bundle_id == "bundle-0"


class TestCostEstimateSerialization:
    def test_round_trip(self) -> None:
        est = CostEstimate(
            estimated_input_tokens=3000,
            estimated_output_tokens=2000,
            estimated_cost_usd=0.039,
        )
        data = est.to_dict()
        restored = CostEstimate.from_dict(data)
        assert restored.estimated_input_tokens == 3000
        assert restored.estimated_cost_usd == 0.039


class TestBudgetLogEntrySerialization:
    def test_round_trip(self) -> None:
        entry = BudgetLogEntry(
            id="log-1",
            cycle_id="cycle-1",
            work_item_id="item-1",
            stage_name="implementation",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            estimated_tokens=5000,
            actual_tokens=4800,
            estimated_cost_usd=0.039,
            actual_cost_usd=0.037,
            logged_at=datetime(2025, 6, 1, 12, 0, 0),
        )
        data = entry.to_dict()
        restored = BudgetLogEntry.from_dict(data)
        assert restored.actual_tokens == 4800
        assert restored.logged_at.year == 2025


class TestCycleSerialization:
    def test_round_trip(self) -> None:
        cycle = Cycle(
            id="cycle-1",
            started_at=datetime(2025, 6, 1, 10, 0, 0),
            items_attempted=3,
            items_completed=2,
            items_shelved=1,
        )
        data = cycle.to_dict()
        restored = Cycle.from_dict(data)
        assert restored.items_attempted == 3
        assert restored.ended_at is None


class TestPipelineContextSerialization:
    def test_round_trip(self) -> None:
        ctx = PipelineContext(
            config={"key": "value"},
            budget_remaining_tokens=10000,
        )
        data = ctx.to_dict()
        restored = PipelineContext.from_dict(data)
        assert restored.budget_remaining_tokens == 10000
