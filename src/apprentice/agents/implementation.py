"""Implementation Agent — ADK LoopAgent with drafter + self-reviewer."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools import exit_loop  # type: ignore[attr-defined]

from apprentice.validators.tools import correctness_validate, lint_validate, stdlib_check

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
Your job is to validate generated code using the available tools.

Steps:
1. First, call save_code with the generated code from the previous message to write it to disk.
2. Use the returned file path to run stdlib_check to verify no third-party imports.
3. Run lint_validate on the same file path to check style and structure.
4. Run correctness_validate on the same file path to verify it executes cleanly.

If ALL validators pass, call the exit_loop tool to finish successfully.
If any validator fails, summarize the issues clearly for the drafter to fix.
Include the specific error messages and suggestions in your feedback.
"""


def save_code(code: str, algorithm_name: str = "algorithm") -> dict[str, Any]:
    """Save generated Python code to a temporary file for validation.

    Args:
        code: The Python source code to save.
        algorithm_name: Name used for the temp file (default: "algorithm").

    Returns:
        Dict with 'path' to the saved file.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"{algorithm_name}.py"
    dest.write_text(code, encoding="utf-8")
    return {"path": str(dest)}


def build_implementation_agent(
    model: LiteLlm,
    max_retries: int = 3,
) -> LoopAgent:
    """Build an ADK LoopAgent for algorithm implementation with self-validation.

    The loop contains:
    1. A drafter LlmAgent that generates algorithm code
    2. A self-reviewer LlmAgent that validates and decides retry/exit

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
        tools=[save_code, lint_validate, correctness_validate, stdlib_check, exit_loop],
        output_key="review_feedback",
    )

    return LoopAgent(
        name="implementation_loop",
        description="Iteratively generates and validates algorithm implementations.",
        max_iterations=max_retries,
        sub_agents=[drafter, reviewer],
    )
