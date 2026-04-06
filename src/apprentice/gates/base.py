"""GateInterface protocol — contract for all quality gates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from apprentice.models.artifact import ArtifactBundle
    from apprentice.models.work_item import GateResult, WorkItem


@runtime_checkable
class GateInterface(Protocol):
    """Contract for all quality gates.

    Gates evaluate artifacts against criteria and return a structured verdict.
    """

    name: str
    max_retries: int
    blocking: bool

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        """Evaluate artifacts against gate criteria."""
        ...
