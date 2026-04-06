"""StageInterface protocol — contract for all pipeline stages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from apprentice.models.budget import CostEstimate
    from apprentice.models.work_item import PipelineContext, StageResult, WorkItem


@runtime_checkable
class StageInterface(Protocol):
    """Contract for all pipeline stages.

    Stages receive a WorkItem and return a StageResult. They must be idempotent —
    safe to retry on the same input without side effects from prior runs.
    """

    name: str

    def execute(self, work_item: WorkItem, context: PipelineContext) -> StageResult:
        """Run the stage and return results with artifacts and diagnostics."""
        ...

    def estimate_cost(self, work_item: WorkItem) -> CostEstimate:
        """Return token/cost estimate before execution for budget pre-checks."""
        ...
