"""CorrectnessValidator — execution-based validation adapted from CorrectnessGate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from apprentice.validators.base import ValidationIssue, ValidationResult

if TYPE_CHECKING:
    from apprentice.models.work_item import WorkItem

_EXECUTION_TIMEOUT = 5  # seconds
_STDERR_EXCERPT_LEN = 300


class CorrectnessValidator:
    """Execute the implementation file and validate it exits cleanly."""

    name = "correctness"

    def validate(self, artifacts: dict[str, str], work_item: WorkItem) -> ValidationResult:
        """Execute the implementation file and check its exit code.

        Args:
            artifacts: Mapping of artifact_type to file path.
            work_item: The work item being validated.

        Returns:
            ValidationResult with issues describing any runtime failures.
        """
        issues: list[ValidationIssue] = []

        path_str = artifacts.get("implementation") or ""
        if not path_str:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                issues=[
                    ValidationIssue(
                        severity="error",
                        message="implementation artifact path is empty",
                        artifact="implementation",
                        suggestion="Provide a non-empty path for the implementation artifact",
                    )
                ],
            )

        path = Path(path_str)
        if not path.exists():
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                issues=[
                    ValidationIssue(
                        severity="error",
                        message=f"implementation file not found: {path_str}",
                        artifact="implementation",
                        suggestion="Generate missing implementation artifact",
                    )
                ],
            )

        source = path.read_text(encoding="utf-8")
        has_main_block = (
            'if __name__ == "__main__"' in source or "if __name__ == '__main__'" in source
        )

        if not has_main_block:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message="no __main__ block found in implementation",
                    artifact="implementation",
                    suggestion=("Add test assertions in an 'if __name__ == \"__main__\":' block"),
                )
            )

        try:
            result = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                timeout=_EXECUTION_TIMEOUT,
                text=True,
            )
        except subprocess.TimeoutExpired:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"execution timed out after {_EXECUTION_TIMEOUT}s",
                    artifact="implementation",
                    suggestion="Optimize algorithm to complete within 5 seconds for test inputs",
                )
            )
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                issues=issues,
            )

        if result.returncode != 0:
            stderr_excerpt = result.stderr[:_STDERR_EXCERPT_LEN].strip()
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"execution failed with return code {result.returncode}",
                    artifact="implementation",
                    suggestion=f"Fix runtime error: {stderr_excerpt}",
                )
            )

        return ValidationResult(
            validator_name=self.name,
            passed=len(issues) == 0,
            issues=issues,
        )
