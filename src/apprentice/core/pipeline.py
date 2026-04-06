"""Config-driven stage sequencer."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from apprentice.core.observability import get_logger, log_gate_result, log_stage_metrics
from apprentice.models.artifact import ArtifactBundle
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
    from apprentice.gates.base import GateInterface
    from apprentice.stages.base import StageInterface

_logger = get_logger(__name__)


@dataclass
class PipelineConfig:
    """Defines stage ordering and gate insertion points."""

    stages: list[str]  # ordered stage names
    gates: dict[str, list[str]]  # gate_name -> runs after these stages
    parallel_stages: list[list[str]]  # groups of stages to run in parallel
    budget_per_stage: int  # token cap per stage


@dataclass
class PipelineResult:
    """Aggregated result of a full pipeline run."""

    work_item: WorkItem
    artifacts: ArtifactBundle
    gate_results: list[GateResult]
    stage_results: list[StageResult]
    total_tokens: int
    total_cost_usd: float
    success: bool


def _merge_stage_result_into_bundle(bundle: ArtifactBundle, result: StageResult) -> None:
    """Write known artifact keys from a StageResult into the bundle in-place."""
    key_to_field: dict[str, str] = {
        "implementation": "implementation_path",
        "instrumented": "instrumented_path",
        "manim_scene": "manim_scene_path",
        "anki_deck": "anki_deck_path",
        "readme_section": "readme_section",
        "pr_url": "pr_url",
    }
    for key, value in result.artifacts.items():
        field = key_to_field.get(key)
        if field is not None:
            setattr(bundle, field, value)


def _gates_for_stage(
    stage_name: str,
    gates_config: dict[str, list[str]],
) -> list[str]:
    """Return gate names that should fire after the given stage completes."""
    return [
        gate_name for gate_name, after_stages in gates_config.items() if stage_name in after_stages
    ]


class Pipeline:
    """Orchestrates stages and gates according to a PipelineConfig."""

    def __init__(
        self,
        stages: dict[str, StageInterface],
        gates: dict[str, GateInterface],
        config: PipelineConfig,
    ) -> None:
        self._stages = stages
        self._gates = gates
        self._config = config

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _budget_pre_check(
        self,
        stage_name: str,
        estimate: CostEstimate,
        context: PipelineContext,
        work_item: WorkItem,
    ) -> bool:
        """Return True if budget is sufficient; False if the item must be shelved."""
        estimated_total = estimate.estimated_input_tokens + estimate.estimated_output_tokens
        if estimated_total > self._config.budget_per_stage:
            _logger.warning(
                "budget_pre_check_failed: estimated tokens exceed per-stage cap",
                extra={
                    "stage_name": stage_name,
                    "estimated_tokens": estimated_total,
                    "budget_per_stage": self._config.budget_per_stage,
                    "work_item_id": work_item.id,
                },
            )
            return False
        if estimated_total > context.budget_remaining_tokens:
            _logger.warning(
                "budget_pre_check_failed: insufficient tokens remaining",
                extra={
                    "stage_name": stage_name,
                    "estimated_tokens": estimated_total,
                    "budget_remaining_tokens": context.budget_remaining_tokens,
                    "work_item_id": work_item.id,
                },
            )
            return False
        if estimate.estimated_cost_usd > context.budget_remaining_usd:
            _logger.warning(
                "budget_pre_check_failed: insufficient USD remaining",
                extra={
                    "stage_name": stage_name,
                    "estimated_cost_usd": estimate.estimated_cost_usd,
                    "budget_remaining_usd": context.budget_remaining_usd,
                    "work_item_id": work_item.id,
                },
            )
            return False
        return True

    def _run_single_stage(
        self,
        stage_name: str,
        work_item: WorkItem,
        context: PipelineContext,
    ) -> StageResult | None:
        """Execute one stage; return StageResult on success or None on exception."""
        stage = self._stages[stage_name]
        start = time.monotonic()
        try:
            result = stage.execute(work_item, context)
        except Exception:
            duration = time.monotonic() - start
            _logger.exception(
                "stage_exception",
                extra={
                    "stage_name": stage_name,
                    "work_item_id": work_item.id,
                    "duration_seconds": duration,
                },
            )
            log_stage_metrics(
                stage_name=stage_name,
                tokens_used=0,
                cost_usd=0.0,
                duration_seconds=duration,
                passed=False,
            )
            return None
        duration = time.monotonic() - start
        log_stage_metrics(
            stage_name=stage_name,
            tokens_used=result.tokens_used,
            cost_usd=result.cost_usd,
            duration_seconds=duration,
            passed=True,
        )
        return result

    def _run_parallel_group(
        self,
        group: list[str],
        work_item: WorkItem,
        context: PipelineContext,
    ) -> list[StageResult]:
        """Run a group of stages concurrently; return all successful results."""
        results: list[StageResult] = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(self._run_single_stage, name, work_item, context): name
                for name in group
                if name in self._stages
            }
            for future in as_completed(futures):
                stage_name = futures[future]
                result = future.result()
                if result is None:
                    _logger.error(
                        "parallel_stage_failed",
                        extra={"stage_name": stage_name, "work_item_id": work_item.id},
                    )
                else:
                    results.append(result)
        return results

    def _evaluate_gates(
        self,
        after_stage: str,
        work_item: WorkItem,
        artifacts: ArtifactBundle,
    ) -> tuple[list[GateResult], bool]:
        """
        Evaluate all gates configured to fire after ``after_stage``.

        Returns (gate_results, should_stop) where should_stop is True when a
        blocking gate FAIL is encountered.
        """
        gate_results: list[GateResult] = []
        for gate_name in _gates_for_stage(after_stage, self._config.gates):
            gate = self._gates.get(gate_name)
            if gate is None:
                _logger.warning(
                    "gate_not_registered",
                    extra={"gate_name": gate_name, "after_stage": after_stage},
                )
                continue
            try:
                result = gate.evaluate(work_item, artifacts)
            except Exception:
                _logger.exception(
                    "gate_exception",
                    extra={"gate_name": gate_name, "work_item_id": work_item.id},
                )
                continue
            log_gate_result(
                gate_name=gate_name,
                verdict=result.verdict.value,
                diagnostics=result.diagnostics,
            )
            gate_results.append(result)
            if result.verdict == GateVerdict.FAIL and gate.blocking:
                _logger.error(
                    "blocking_gate_failed",
                    extra={
                        "gate_name": gate_name,
                        "work_item_id": work_item.id,
                        "diagnostics": result.diagnostics,
                    },
                )
                return gate_results, True
            if result.verdict == GateVerdict.WARN:
                _logger.warning(
                    "gate_warning",
                    extra={
                        "gate_name": gate_name,
                        "work_item_id": work_item.id,
                        "diagnostics": result.diagnostics,
                    },
                )
        return gate_results, False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, work_item: WorkItem, context: PipelineContext) -> PipelineResult:
        """Orchestrate the full pipeline and return a PipelineResult."""
        work_item.status = WorkItemStatus.IN_PROGRESS

        bundle = ArtifactBundle(
            id=str(uuid.uuid4()),
            work_item_id=work_item.id,
        )

        all_stage_results: list[StageResult] = []
        all_gate_results: list[GateResult] = []
        total_tokens = 0
        total_cost_usd = 0.0

        # Build a set of stage names that belong to parallel groups so we
        # can skip them when iterating the sequential stage list.
        parallel_stage_names: set[str] = {
            name for group in self._config.parallel_stages for name in group
        }

        def _process_stage_result(
            stage_name: str,
            result: StageResult | None,
        ) -> bool:
            """Update shared state and evaluate gates. Return False to stop pipeline."""
            nonlocal total_tokens, total_cost_usd
            if result is None:
                return True  # stage exception already logged; continue pipeline
            context.budget_remaining_tokens -= result.tokens_used
            context.budget_remaining_usd -= result.cost_usd
            total_tokens += result.tokens_used
            total_cost_usd += result.cost_usd
            work_item.actual_tokens += result.tokens_used
            _merge_stage_result_into_bundle(bundle, result)
            all_stage_results.append(result)

            gate_results, should_stop = self._evaluate_gates(stage_name, work_item, bundle)
            all_gate_results.extend(gate_results)
            return not should_stop

        for stage_name in self._config.stages:
            # Parallel groups are handled separately below; skip individual names.
            if stage_name in parallel_stage_names:
                continue

            stage = self._stages.get(stage_name)
            if stage is None:
                _logger.warning(
                    "stage_not_registered",
                    extra={"stage_name": stage_name, "work_item_id": work_item.id},
                )
                continue

            # Budget pre-check
            try:
                estimate: CostEstimate = stage.estimate_cost(work_item)
            except Exception:
                _logger.exception(
                    "estimate_cost_exception",
                    extra={"stage_name": stage_name, "work_item_id": work_item.id},
                )
                estimate = CostEstimate(
                    estimated_input_tokens=0,
                    estimated_output_tokens=0,
                    estimated_cost_usd=0.0,
                )

            if not self._budget_pre_check(stage_name, estimate, context, work_item):
                work_item.status = WorkItemStatus.SHELVED
                work_item.last_failed_stage = stage_name
                return PipelineResult(
                    work_item=work_item,
                    artifacts=bundle,
                    gate_results=all_gate_results,
                    stage_results=all_stage_results,
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost_usd,
                    success=False,
                )

            result = self._run_single_stage(stage_name, work_item, context)
            if not _process_stage_result(stage_name, result):
                work_item.status = WorkItemStatus.SHELVED
                work_item.last_failed_stage = stage_name
                return PipelineResult(
                    work_item=work_item,
                    artifacts=bundle,
                    gate_results=all_gate_results,
                    stage_results=all_stage_results,
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost_usd,
                    success=False,
                )

        # Run parallel groups
        for group in self._config.parallel_stages:
            # Validate budget against each stage in the group individually
            for stage_name in group:
                stage = self._stages.get(stage_name)
                if stage is None:
                    continue
                try:
                    estimate = stage.estimate_cost(work_item)
                except Exception:
                    _logger.exception(
                        "estimate_cost_exception",
                        extra={"stage_name": stage_name, "work_item_id": work_item.id},
                    )
                    estimate = CostEstimate(
                        estimated_input_tokens=0,
                        estimated_output_tokens=0,
                        estimated_cost_usd=0.0,
                    )
                if not self._budget_pre_check(stage_name, estimate, context, work_item):
                    work_item.status = WorkItemStatus.SHELVED
                    work_item.last_failed_stage = stage_name
                    return PipelineResult(
                        work_item=work_item,
                        artifacts=bundle,
                        gate_results=all_gate_results,
                        stage_results=all_stage_results,
                        total_tokens=total_tokens,
                        total_cost_usd=total_cost_usd,
                        success=False,
                    )

            parallel_results = self._run_parallel_group(group, work_item, context)
            for stage_result in parallel_results:
                # Evaluate gates per stage within the group
                if not _process_stage_result(stage_result.stage_name, stage_result):
                    work_item.status = WorkItemStatus.SHELVED
                    work_item.last_failed_stage = stage_result.stage_name
                    return PipelineResult(
                        work_item=work_item,
                        artifacts=bundle,
                        gate_results=all_gate_results,
                        stage_results=all_stage_results,
                        total_tokens=total_tokens,
                        total_cost_usd=total_cost_usd,
                        success=False,
                    )

        work_item.status = WorkItemStatus.COMPLETED
        work_item.completed_at = datetime.now()

        return PipelineResult(
            work_item=work_item,
            artifacts=bundle,
            gate_results=all_gate_results,
            stage_results=all_stage_results,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            success=True,
        )
