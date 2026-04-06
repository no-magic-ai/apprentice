"""Correctness gate — reference test execution, 1 retry with re-prompt."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.models.artifact import ArtifactBundle

from apprentice.models.work_item import GateResult, GateVerdict, WorkItem

_EXECUTION_TIMEOUT = 5  # seconds


class CorrectnessGate:
    """Execute the implementation file and validate it exits cleanly."""

    name = "correctness"
    max_retries = 1
    blocking = True

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        """Execute the implementation file and check its exit code.

        Args:
            work_item: The work item being evaluated.
            artifacts: Bundle of generated artifacts.

        Returns:
            GateResult with PASS or FAIL and stdout/stderr diagnostics.
        """
        diagnostics: dict[str, Any] = {}

        path_str = artifacts.implementation_path
        if not path_str:
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={"error": "implementation_path is empty"},
            )

        path = Path(path_str)
        if not path.exists():
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={"error": f"file not found: {path_str}"},
            )

        source = path.read_text(encoding="utf-8")
        has_main_block = (
            'if __name__ == "__main__"' in source or "if __name__ == '__main__'" in source
        )
        diagnostics["has_main_block"] = has_main_block

        try:
            result = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                timeout=_EXECUTION_TIMEOUT,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={
                    **diagnostics,
                    "error": f"execution timed out after {_EXECUTION_TIMEOUT}s",
                },
            )

        diagnostics["return_code"] = result.returncode
        diagnostics["stdout"] = result.stdout
        diagnostics["stderr"] = result.stderr

        verdict = GateVerdict.PASS if result.returncode == 0 else GateVerdict.FAIL
        return GateResult(
            gate_name=self.name,
            verdict=verdict,
            diagnostics=diagnostics,
        )
