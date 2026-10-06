"""Tests for the pipeline orchestrator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from apprentice.core.artifacts import ArtifactError
from apprentice.core.pipeline import Pipeline, PipelineConfig, PipelineResult
from apprentice.models.artifact import ArtifactBundle  # noqa: TC001
from apprentice.models.budget import CostEstimate
from apprentice.models.work_item import (
    GateResult,
    GateVerdict,
    PipelineContext,
    StageResult,
    WorkItem,
    WorkItemStatus,
)

if TYPE_CHECKING:
    from pathlib import Path

    from apprentice.core.session_store import SessionStore


class _StubStage:
    """Minimal stage for testing pipeline orchestration."""

    def __init__(self, name: str, tokens: int = 100) -> None:
        self.name = name
        self._tokens = tokens

    def estimate_cost(self, work_item: WorkItem) -> CostEstimate:
        return CostEstimate(
            estimated_input_tokens=self._tokens,
            estimated_output_tokens=0,
            estimated_cost_usd=0.001,
        )

    def execute(self, work_item: WorkItem, context: PipelineContext) -> StageResult:
        return StageResult(
            stage_name=self.name,
            artifacts={"implementation": f"/tmp/{self.name}.py"},
            tokens_used=self._tokens,
            cost_usd=0.001,
        )


class _FailingStage(_StubStage):
    def execute(self, work_item: WorkItem, context: PipelineContext) -> StageResult:
        raise RuntimeError("stage failure")


class _StubGate:
    """Minimal gate for testing pipeline orchestration."""

    def __init__(self, name: str, verdict: GateVerdict = GateVerdict.PASS) -> None:
        self.name = name
        self.max_retries = 0
        self.blocking = True
        self._verdict = verdict

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        return GateResult(gate_name=self.name, verdict=self._verdict)


def _make_pipeline(
    stages: dict[str, Any] | None = None,
    gates: dict[str, Any] | None = None,
    parallel: list[list[str]] | None = None,
) -> Pipeline:
    if stages is None:
        stages = {"impl": _StubStage("impl"), "validate": _StubStage("validate")}
    if gates is None:
        gates = {}
    config = PipelineConfig(
        stages=list(stages.keys()),
        gates={g: list(stages.keys()) for g in gates} if gates else {},
        parallel_stages=parallel or [],
        budget_per_stage=20_000,
    )
    return Pipeline(stages=stages, gates=gates, config=config)


def _make_context(store: SessionStore, tokens: int = 100_000) -> PipelineContext:
    return PipelineContext(
        config={"provider": None, "artifacts": {}},
        budget_remaining_tokens=tokens,
        budget_remaining_usd=5.0,
        artifact_root=str(store.allocate_work_root()),
    )


class TestPipelineBasic:
    def test_successful_run(self, store: SessionStore) -> None:
        pipeline = _make_pipeline()
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        result = pipeline.run(item, _make_context(store))
        assert isinstance(result, PipelineResult)
        assert result.success is True
        assert item.status == WorkItemStatus.COMPLETED
        assert result.total_tokens == 200  # two stages x 100

    def test_returns_stage_results(self, store: SessionStore) -> None:
        pipeline = _make_pipeline()
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        result = pipeline.run(item, _make_context(store))
        assert len(result.stage_results) == 2


class TestPipelineGates:
    def test_passing_gate(self, store: SessionStore) -> None:
        stages: dict[str, Any] = {"impl": _StubStage("impl")}
        gates: dict[str, Any] = {"lint": _StubGate("lint")}
        config = PipelineConfig(
            stages=["impl"],
            gates={"lint": ["impl"]},
            parallel_stages=[],
            budget_per_stage=20_000,
        )
        pipeline = Pipeline(stages=stages, gates=gates, config=config)
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        result = pipeline.run(item, _make_context(store))
        assert result.success is True
        assert len(result.gate_results) == 1

    def test_failing_blocking_gate_shelves(self, store: SessionStore) -> None:
        stages: dict[str, Any] = {"impl": _StubStage("impl")}
        gates: dict[str, Any] = {"lint": _StubGate("lint", GateVerdict.FAIL)}
        config = PipelineConfig(
            stages=["impl"],
            gates={"lint": ["impl"]},
            parallel_stages=[],
            budget_per_stage=20_000,
        )
        pipeline = Pipeline(stages=stages, gates=gates, config=config)
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        result = pipeline.run(item, _make_context(store))
        assert result.success is False
        assert item.status == WorkItemStatus.SHELVED

    def test_warning_gate_continues(self, store: SessionStore) -> None:
        stages: dict[str, Any] = {"impl": _StubStage("impl")}
        gates: dict[str, Any] = {"lint": _StubGate("lint", GateVerdict.WARN)}
        config = PipelineConfig(
            stages=["impl"],
            gates={"lint": ["impl"]},
            parallel_stages=[],
            budget_per_stage=20_000,
        )
        pipeline = Pipeline(stages=stages, gates=gates, config=config)
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        result = pipeline.run(item, _make_context(store))
        assert result.success is True


class TestPipelineBudget:
    def test_shelves_on_insufficient_budget(self, store: SessionStore) -> None:
        pipeline = _make_pipeline()
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        ctx = _make_context(store, tokens=50)  # less than stage estimate
        result = pipeline.run(item, ctx)
        assert result.success is False
        assert item.status == WorkItemStatus.SHELVED


class TestPipelineParallel:
    def test_parallel_stages_run(self, store: SessionStore) -> None:
        stages: dict[str, Any] = {
            "impl": _StubStage("impl"),
            "instr": _StubStage("instr"),
            "viz": _StubStage("viz"),
            "assess": _StubStage("assess"),
        }
        config = PipelineConfig(
            stages=["impl", "instr", "viz", "assess"],
            gates={},
            parallel_stages=[["instr", "viz", "assess"]],
            budget_per_stage=20_000,
        )
        pipeline = Pipeline(stages=stages, gates={}, config=config)
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        result = pipeline.run(item, _make_context(store))
        assert result.success is True
        assert len(result.stage_results) == 4
        assert result.total_tokens == 400


class TestPipelineStageFailure:
    def test_stage_exception_continues(self, store: SessionStore) -> None:
        stages: dict[str, Any] = {
            "impl": _FailingStage("impl"),
            "validate": _StubStage("validate"),
        }
        pipeline = _make_pipeline(stages=stages)
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        result = pipeline.run(item, _make_context(store))
        # Pipeline continues after stage failure
        assert result.success is True
        assert len(result.stage_results) == 1  # only validate succeeded


class _RecordingStage(_StubStage):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.executed = 0

    def execute(self, work_item: WorkItem, context: PipelineContext) -> StageResult:
        self.executed += 1
        return super().execute(work_item, context)


class TestPipelineArtifactRoot:
    def _refused(self, context: PipelineContext) -> str:
        stage = _RecordingStage("implementation")
        pipeline = Pipeline(
            stages={"implementation": stage},
            gates={},
            config=PipelineConfig(
                stages=["implementation"], gates={}, parallel_stages=[], budget_per_stage=1
            ),
        )
        with pytest.raises(ArtifactError) as excinfo:
            pipeline.run(WorkItem(id="t", algorithm_name="test", tier=1), context)
        assert stage.executed == 0
        return str(excinfo.value)

    def test_unset_root_fails_before_any_stage(self) -> None:
        assert "no artifact root" in self._refused(PipelineContext(budget_remaining_tokens=10_000))

    def test_nonexistent_root_fails_before_any_stage(self, tmp_path: Path) -> None:
        error = self._refused(PipelineContext(artifact_root=str(tmp_path / "missing")))
        assert "does not exist" in error

    def test_symlink_root_fails_before_any_stage(self, store: SessionStore, tmp_path: Path) -> None:
        link = tmp_path / "link"
        link.symlink_to(store.allocate_work_root())
        assert "symlink" in self._refused(PipelineContext(artifact_root=str(link)))

    def test_previously_populated_root_is_not_reused(self, store: SessionStore) -> None:
        root = store.allocate_work_root()
        (root / "implementation.py").write_text("old = 1\n")
        error = self._refused(PipelineContext(artifact_root=str(root)))
        assert "already holds artifacts" in error
