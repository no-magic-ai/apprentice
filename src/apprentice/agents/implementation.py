"""ImplementationAgent — self-validating algorithm code generator with retry loop."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from apprentice.stages.implementation import (
    _check_stdlib_only,
    _Completion,
    _extract_code_block,
)

if TYPE_CHECKING:
    from apprentice.models.agent import AgentContext, AgentResult, AgentTask
    from apprentice.models.work_item import WorkItem
    from apprentice.validators.base import ValidationIssue, ValidationResult
# isort: split

# Hardcoded Sonnet rates — mirrors stages/implementation.py
_INPUT_RATE_USD: float = 3.0 / 1_000_000
_OUTPUT_RATE_USD: float = 15.0 / 1_000_000

# Generous token ceiling for generation calls
_MAX_TOKENS: int = 24_000


class ImplementationAgent:
    """Generate a stdlib-only Python algorithm implementation with self-validation.

    Runs a retry loop: generate → lint → correctness → succeed or retry with
    failure context injected into the next prompt.

    Attributes:
        name: Agent identifier used by the orchestrator.
        role: Human-readable description of this agent's responsibility.
        system_prompt: Persistent instruction injected into every completion call.
        allowed_tools: Tools this agent is permitted to invoke.
    """

    name: str = "implementation"
    role: str = "Expert algorithm implementer"
    system_prompt: str = (
        "You are an expert algorithm implementer for the no-magic educational project. "
        "You write clean, well-documented Python implementations with:\n"
        "- Type hints on all function signatures\n"
        "- Google-style docstrings with Args, Returns, Complexity sections\n"
        "- Zero external dependencies (stdlib only)\n"
        "- Inline comments explaining key algorithmic decisions\n"
        "- Reference test cases as a __main__ block"
    )
    allowed_tools: ClassVar[list[str]] = [
        "llm_complete",
        "lint_validate",
        "correctness_validate",
        "ast_analyze",
    ]

    def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        """Run the generate-validate-retry loop and return a structured result.

        Args:
            task: Task specification including work item and constraints.
            context: Runtime context carrying the provider and budget info.

        Returns:
            AgentResult with success status, artifact path, token usage, and
            diagnostics. On failure returns all collected validation issues.
        """
        from apprentice.models.agent import AgentResult

        max_retries: int = int(task.constraints.get("max_retries", 3))
        total_tokens: int = 0
        total_cost: float = 0.0
        all_diagnostics: list[dict[str, Any]] = []
        previous_code: str = ""
        previous_issues: list[ValidationIssue] = []

        for attempt in range(1, max_retries + 1):
            prompt = self._build_prompt(
                task.work_item,
                attempt,
                previous_code,
                previous_issues,
            )

            completion = self._generate(prompt, context)
            total_tokens += completion.input_tokens + completion.output_tokens
            total_cost += (
                completion.input_tokens * _INPUT_RATE_USD
                + completion.output_tokens * _OUTPUT_RATE_USD
            )

            code = _extract_code_block(completion.text)
            previous_code = code

            non_stdlib = _check_stdlib_only(code)
            if non_stdlib:
                all_diagnostics.append(
                    {
                        "attempt": attempt,
                        "level": "warning",
                        "message": "non-stdlib imports detected",
                        "imports": non_stdlib,
                    }
                )

            artifact_path = _write_temp_artifact(task.work_item.algorithm_name, code)

            lint_result = _run_lint(artifact_path, task.work_item)
            if not lint_result.passed:
                all_diagnostics.extend(_issues_to_dicts(lint_result.issues, attempt, "lint"))
                if attempt < max_retries:
                    previous_issues = lint_result.issues
                    continue
                return AgentResult(
                    agent_name=self.name,
                    task_id=task.task_id,
                    success=False,
                    artifacts={},
                    tokens_used=total_tokens,
                    cost_usd=round(total_cost, 6),
                    diagnostics=all_diagnostics,
                    attempt_number=attempt,
                )

            correctness_result = _run_correctness(artifact_path, task.work_item)
            if not correctness_result.passed:
                all_diagnostics.extend(
                    _issues_to_dicts(correctness_result.issues, attempt, "correctness")
                )
                if attempt < max_retries:
                    previous_issues = correctness_result.issues
                    continue
                return AgentResult(
                    agent_name=self.name,
                    task_id=task.task_id,
                    success=False,
                    artifacts={},
                    tokens_used=total_tokens,
                    cost_usd=round(total_cost, 6),
                    diagnostics=all_diagnostics,
                    attempt_number=attempt,
                )

            # All validators passed.
            return AgentResult(
                agent_name=self.name,
                task_id=task.task_id,
                success=True,
                artifacts={"implementation": artifact_path},
                tokens_used=total_tokens,
                cost_usd=round(total_cost, 6),
                diagnostics=all_diagnostics,
                attempt_number=attempt,
            )

        # Should be unreachable — the loop always returns on the last attempt.
        from apprentice.models.agent import AgentResult

        return AgentResult(
            agent_name=self.name,
            task_id=task.task_id,
            success=False,
            artifacts={},
            tokens_used=total_tokens,
            cost_usd=round(total_cost, 6),
            diagnostics=all_diagnostics,
            attempt_number=max_retries,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        work_item: WorkItem,
        attempt: int,
        previous_code: str,
        previous_issues: list[ValidationIssue],
    ) -> str:
        """Build the generation prompt, injecting failure context on retries.

        Args:
            work_item: Algorithm specification.
            attempt: Current attempt number (1-based).
            previous_code: Code generated on the prior attempt; empty on first.
            previous_issues: Validation issues from the prior attempt; empty on first.

        Returns:
            Formatted prompt string ready for the provider.
        """
        if attempt == 1 or not previous_issues:
            return (
                f"Generate a Python implementation of the **{work_item.algorithm_name}** "
                f"algorithm (tier {work_item.tier}).\n\n"
                f"**Rationale / description:** {work_item.rationale or 'N/A'}\n\n"
                "## Requirements\n\n"
                "- Standard library only — zero third-party imports.\n"
                "- Full type annotations on every function and method.\n"
                "- Google-style docstrings on every public symbol.\n"
                "- Include inline test cases using `doctest` in the module docstring.\n"
                "- Single file, no global mutable state, idempotent functions.\n\n"
                "Return **only** the Python source code inside a ```python ... ``` fence."
            )

        issues_text = "\n".join(
            f"- [{issue.severity}] {issue.message}\n  Suggestion: {issue.suggestion}"
            for issue in previous_issues
        )
        return (
            f"Your previous implementation of **{work_item.algorithm_name}** "
            f"(tier {work_item.tier}) had the following issues:\n\n"
            f"{issues_text}\n\n"
            "Previous code:\n"
            "```python\n"
            f"{previous_code}\n"
            "```\n\n"
            "Please fix ALL listed issues and return the corrected implementation.\n"
            "Return **only** the Python source code inside a ```python ... ``` fence."
        )

    def _generate(self, prompt: str, context: AgentContext) -> _Completion:
        """Invoke the provider to generate a completion.

        Args:
            prompt: Fully constructed prompt string.
            context: Agent context carrying the provider instance.

        Returns:
            _Completion with text and token counts.

        Raises:
            RuntimeError: If context.provider is not set or lacks a complete() method.
        """
        provider = context.provider
        if provider is None or not hasattr(provider, "complete"):
            raise RuntimeError(
                "No provider configured. AgentContext.provider must be a ProviderInterface instance."
            )
        result = provider.complete(prompt, {}, _MAX_TOKENS)
        return _Completion(
            text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _write_temp_artifact(algorithm_name: str, code: str) -> str:
    """Write code to a temp file and return the absolute path.

    Args:
        algorithm_name: Used as the filename stem.
        code: Python source to persist.

    Returns:
        Absolute path string of the written file.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"{algorithm_name}.py"
    dest.write_text(code, encoding="utf-8")
    return str(dest)


def _run_lint(artifact_path: str, work_item: WorkItem) -> ValidationResult:
    """Instantiate LintValidator and validate the artifact.

    Args:
        artifact_path: Absolute path to the generated Python file.
        work_item: Algorithm work item providing validation context.

    Returns:
        ValidationResult from LintValidator.
    """
    from apprentice.validators.lint import LintValidator

    validator = LintValidator()
    return validator.validate({"implementation": artifact_path}, work_item)


def _run_correctness(artifact_path: str, work_item: WorkItem) -> ValidationResult:
    """Instantiate CorrectnessValidator and validate the artifact.

    Args:
        artifact_path: Absolute path to the generated Python file.
        work_item: Algorithm work item providing validation context.

    Returns:
        ValidationResult from CorrectnessValidator.
    """
    from apprentice.validators.correctness import CorrectnessValidator

    validator = CorrectnessValidator()
    return validator.validate({"implementation": artifact_path}, work_item)


def _issues_to_dicts(
    issues: list[ValidationIssue],
    attempt: int,
    validator: str,
) -> list[dict[str, Any]]:
    """Convert ValidationIssue list to plain dicts for AgentResult.diagnostics.

    Args:
        issues: Validation issues to convert.
        attempt: Attempt number to attach to each diagnostic.
        validator: Validator name tag (e.g. "lint", "correctness").

    Returns:
        List of plain diagnostic dicts.
    """
    return [
        {
            "attempt": attempt,
            "validator": validator,
            "severity": issue.severity,
            "message": issue.message,
            "artifact": issue.artifact,
            "suggestion": issue.suggestion,
        }
        for issue in issues
    ]
