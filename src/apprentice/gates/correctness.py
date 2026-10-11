"""Correctness gate — runs the implementation in the sandbox and requires a clean exit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apprentice.models.work_item import GateResult, GateVerdict, WorkItem
from apprentice.validators.correctness import output_excerpt, run_implementation

if TYPE_CHECKING:
    from pathlib import Path

    from apprentice.models.artifact import ArtifactBundle
    from apprentice.sandbox.docker import Sandbox


class CorrectnessGate:
    """Run the implementation file in the sandbox and validate it exits cleanly.

    `root` is the trusted artifact root the bundle's implementation must be
    in; nothing runs on the host.
    """

    name = "correctness"
    max_retries = 1
    blocking = True

    def __init__(self, sandbox: Sandbox, root: Path) -> None:
        self._sandbox = sandbox
        self._root = root

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        """Run the implementation file and check its exit status.

        Args:
            work_item: The work item being evaluated.
            artifacts: Bundle of generated artifacts.

        Returns:
            GateResult with PASS or FAIL and the bounded program output.

        Raises:
            UnsafeInputError: If the implementation is outside the root or
                names no stageable file.
            SandboxInfrastructureError: If the program could not be run safely.
        """
        path = artifacts.implementation_path
        if not path:
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={"error": "implementation_path is empty"},
            )
        checked = run_implementation(self._sandbox, self._root, path)
        if checked is None:
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={"error": f"file not found: {path}"},
            )
        run = checked.run
        diagnostics: dict[str, Any] = {
            "has_main_block": checked.has_main_block,
            "return_code": run.returncode,
            "limit": run.limit,
            "output": output_excerpt(run.output),
        }
        if not run.passed:
            diagnostics["error"] = f"the program {run.problem()}"
        return GateResult(
            gate_name=self.name,
            verdict=GateVerdict.PASS if run.passed else GateVerdict.FAIL,
            diagnostics=diagnostics,
        )
