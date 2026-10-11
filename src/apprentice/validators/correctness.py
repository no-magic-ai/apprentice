"""CorrectnessValidator — runs the implementation in the sandbox and checks it exits cleanly.

The implementation is never executed on the host. Its bytes are staged from
the caller-authorized artifact root (never following links) and run in the
configured execution sandbox under fixed time, output, memory and process
limits. A program that fails inside those limits is an ordinary validation
failure; a backend, staging or teardown fault raises
`SandboxInfrastructureError` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apprentice.sandbox.staging import MAX_ARTIFACT_BYTES, read_scoped, scope_label
from apprentice.validators.base import ValidationIssue, ValidationResult

if TYPE_CHECKING:
    from pathlib import Path

    from apprentice.models.work_item import WorkItem
    from apprentice.sandbox.docker import ProgramRun, Sandbox

OUTPUT_EXCERPT_LEN = 300


@dataclass(frozen=True)
class ImplementationRun:
    """The staged implementation's `__main__` check and its contained run."""

    has_main_block: bool
    run: ProgramRun


def run_implementation(sandbox: Sandbox, root: Path, path: str) -> ImplementationRun | None:
    """Stage `path` from `root` and run it in `sandbox`; None when the file is absent.

    Raises:
        UnsafeInputError: If `path` is outside `root` or names no stageable file.
        SandboxInfrastructureError: If staging, the backend or teardown failed.
    """
    source = read_scoped(root, path, limit=MAX_ARTIFACT_BYTES)
    if source is None:
        return None
    text = source.decode("utf-8", "replace")
    has_main_block = 'if __name__ == "__main__"' in text or "if __name__ == '__main__'" in text
    return ImplementationRun(has_main_block, sandbox.run_program(source, scope=scope_label(root)))


def output_excerpt(output: str) -> str:
    """The end of a program's output, where a traceback or failed assertion is reported."""
    return output.strip()[-OUTPUT_EXCERPT_LEN:]


class CorrectnessValidator:
    """Run the implementation file in the sandbox and validate it exits cleanly.

    `root` is the trusted artifact root the implementation path must be in
    (a run's work root); `sandbox` is the verified execution backend.
    """

    name = "correctness"

    def __init__(self, sandbox: Sandbox, root: Path) -> None:
        self._sandbox = sandbox
        self._root = root

    def validate(self, artifacts: dict[str, str], work_item: WorkItem) -> ValidationResult:
        """Run the implementation file and check its exit status.

        Args:
            artifacts: Mapping of artifact_type to a file path inside the root.
            work_item: The work item being validated.

        Returns:
            ValidationResult with issues describing any runtime failures.

        Raises:
            UnsafeInputError: If the implementation path is outside the root or
                names no stageable file.
            SandboxInfrastructureError: If the program could not be run safely.
        """
        path = artifacts.get("implementation") or ""
        if not path:
            return self._failed(
                "implementation artifact path is empty",
                "Provide a non-empty path for the implementation artifact",
            )
        checked = run_implementation(self._sandbox, self._root, path)
        if checked is None:
            return self._failed(
                f"implementation file not found: {path}", "Generate missing implementation artifact"
            )

        issues: list[ValidationIssue] = []
        if not checked.has_main_block:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message="no __main__ block found in implementation",
                    artifact="implementation",
                    suggestion="Add test assertions in an 'if __name__ == \"__main__\":' block",
                )
            )
        run = checked.run
        if run.limit == "time":
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"execution {run.problem()}",
                    artifact="implementation",
                    suggestion=(
                        f"Optimize algorithm to complete within {run.profile.program_seconds} "
                        "seconds for test inputs"
                    ),
                )
            )
        elif not run.passed:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"execution failed: the program {run.problem()}",
                    artifact="implementation",
                    suggestion=f"Fix runtime error: {output_excerpt(run.output)}",
                )
            )
        return ValidationResult(validator_name=self.name, passed=not issues, issues=issues)

    def _failed(self, message: str, suggestion: str) -> ValidationResult:
        return ValidationResult(
            validator_name=self.name,
            passed=False,
            issues=[
                ValidationIssue(
                    severity="error",
                    message=message,
                    artifact="implementation",
                    suggestion=suggestion,
                )
            ],
        )
