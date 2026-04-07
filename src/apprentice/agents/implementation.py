"""Implementation Agent — ADK LoopAgent with drafter + self-reviewer."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools import exit_loop  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_DRAFTER_INSTRUCTION = """\
You are an expert algorithm implementer for the no-magic educational project.
You write clean, well-documented Python implementations with:
- Type hints on all function signatures
- Google-style docstrings with Args, Returns, Complexity sections
- Zero external dependencies (stdlib only)
- Inline comments explaining key algorithmic decisions
- Reference test cases in an `if __name__ == "__main__":` block

Generate the algorithm implementation based on the task description.
If the conversation history contains feedback from a previous review,
fix ALL listed issues in your new implementation.

Write the complete Python source code. Do NOT use markdown fences.
Write ONLY the Python source code, nothing else.
"""

_REVIEWER_INSTRUCTION = """\
You are a code reviewer for algorithm implementations.

Call validate_implementation with the COMPLETE Python source code from the \
drafter's previous message. The tool saves the code to disk and runs all \
validators (stdlib-only imports, lint, correctness) in one step.

If the tool returns all_passed=true, call exit_loop immediately.
If any validator failed, respond with a summary of the failures for the drafter.
"""


def validate_implementation(code: str, algorithm_name: str = "algorithm") -> dict[str, Any]:
    """Save code to disk and run all validators in a single call.

    Writes the code to a temp file, then runs stdlib check, lint validation,
    and correctness validation. Returns combined results.

    Args:
        code: Complete Python source code to validate.
        algorithm_name: Name for the temp file (default: "algorithm").

    Returns:
        Dict with 'all_passed' bool, 'file_path', and per-validator results.
    """
    from apprentice.validators.tools import correctness_validate, lint_validate, stdlib_check

    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"{algorithm_name}.py"
    dest.write_text(code, encoding="utf-8")
    file_path = str(dest)

    stdlib_result = stdlib_check(file_path)
    lint_result = lint_validate(file_path)
    correctness_result = correctness_validate(file_path)

    all_passed = stdlib_result["passed"] and lint_result["passed"] and correctness_result["passed"]

    return {
        "all_passed": all_passed,
        "file_path": file_path,
        "stdlib": stdlib_result,
        "lint": lint_result,
        "correctness": correctness_result,
    }


def build_implementation_agent(
    model: LiteLlm,
    max_retries: int = 3,
) -> LoopAgent:
    """Build an ADK LoopAgent for algorithm implementation with self-validation.

    The loop contains:
    1. A drafter LlmAgent that generates algorithm code
    2. A self-reviewer LlmAgent that validates with a single composite tool

    Args:
        model: LiteLlm model instance for both sub-agents.
        max_retries: Maximum loop iterations before giving up.

    Returns:
        A configured LoopAgent ready for pipeline integration.
    """
    drafter = LlmAgent(
        name="drafter",
        model=model,
        instruction=_DRAFTER_INSTRUCTION,
        output_key="generated_code",
    )

    reviewer = LlmAgent(
        name="self_reviewer",
        model=model,
        instruction=_REVIEWER_INSTRUCTION,
        tools=[validate_implementation, exit_loop],
        output_key="review_feedback",
    )

    return LoopAgent(
        name="implementation_loop",
        description="Iteratively generates and validates algorithm implementations.",
        max_iterations=max_retries,
        sub_agents=[drafter, reviewer],
    )
