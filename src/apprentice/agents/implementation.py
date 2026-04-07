"""Implementation Agent — ADK LoopAgent with LLM drafter + programmatic validation."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent, LoopAgent

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_DRAFTER_INSTRUCTION = """\
You are an expert algorithm implementer for the no-magic educational project.

Write a complete Python implementation with:
- Module-level docstring with summary, Args, Returns, Complexity, References sections
- Type hints on all function signatures
- Google-style docstrings on all public functions
- Zero external dependencies (stdlib only)
- Inline comments explaining key algorithmic decisions
- Test assertions in an `if __name__ == "__main__":` block

If the session state contains validation_feedback, your previous attempt failed.
Fix ALL issues listed in the feedback.

Write the complete Python source code. Do NOT use markdown fences.
Write ONLY the Python source code, nothing else.
"""


def _run_validators(code: str, algorithm_name: str) -> dict[str, Any]:
    """Run all validators on code and return combined results."""
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

    failures: list[str] = []
    if not stdlib_result["passed"]:
        violations = stdlib_result.get("violations", [])
        failures.append(f"Non-stdlib imports: {violations}")
    if not lint_result["passed"]:
        for issue in lint_result.get("issues", []):
            failures.append(f"Lint: {issue.get('message', '')} — {issue.get('suggestion', '')}")
    if not correctness_result["passed"]:
        for issue in correctness_result.get("issues", []):
            failures.append(
                f"Correctness: {issue.get('message', '')} — {issue.get('suggestion', '')}"
            )

    return {
        "all_passed": all_passed,
        "file_path": file_path,
        "failures": failures,
    }


def _make_after_drafter_callback() -> Any:
    """Create a callback that validates the drafter's output after each generation.

    If validation passes, sets _implementation_passed=True in state so the
    LoopAgent's exit condition (checked via a trivial checker agent) fires.
    If validation fails, writes feedback to session state for the next iteration.
    """

    async def after_drafter(callback_context: Any) -> Any:
        state = callback_context.state
        code = state.get("generated_code", "")
        algorithm_name = state.get("algorithm_name", "algorithm")

        if not code:
            state["validation_feedback"] = (
                "No code was generated. Write complete Python source code."
            )
            return None

        result = _run_validators(code, algorithm_name)

        if result["all_passed"]:
            state["validation_feedback"] = ""
            state["implementation_path"] = result["file_path"]
        else:
            feedback = "Your implementation has the following issues:\n"
            feedback += "\n".join(f"- {f}" for f in result["failures"])
            feedback += "\n\nFix ALL issues and rewrite the complete implementation."
            state["validation_feedback"] = feedback

        return None

    return after_drafter


def _make_exit_condition() -> Any:
    """Create a callback that exits the loop when validation passes.

    Checked before each iteration. If validation_feedback is empty (meaning
    the previous iteration passed), exits the loop.
    """

    async def should_exit(callback_context: Any) -> Any:
        from google.genai import types

        state = callback_context.state
        feedback = state.get("validation_feedback")

        # On first iteration, validation_feedback doesn't exist yet — continue
        if feedback is None:
            return None

        # Empty feedback means validation passed — exit
        if feedback == "":
            return types.Content(
                role="model",
                parts=[types.Part(text="Implementation validated successfully.")],
            )

        # Non-empty feedback means validation failed — continue loop
        return None

    return should_exit


def build_implementation_agent(
    model: LiteLlm,
    max_retries: int = 3,
) -> LoopAgent:
    """Build an ADK LoopAgent for algorithm implementation with programmatic validation.

    Architecture:
    - Single LlmAgent (drafter) generates code. 1 LLM call per iteration.
    - after_agent_callback runs validators programmatically (no LLM needed).
    - If validation fails, feedback is written to session state.
    - The drafter reads {validation_feedback} on the next iteration.
    - before_agent_callback on the LoopAgent exits when validation passes.

    This uses 1 LLM call per iteration instead of 5+ with an LLM reviewer.

    Args:
        model: LiteLlm model instance.
        max_retries: Maximum loop iterations before giving up.

    Returns:
        A configured LoopAgent ready for pipeline integration.
    """
    drafter = LlmAgent(
        name="drafter",
        model=model,
        instruction=_DRAFTER_INSTRUCTION,
        output_key="generated_code",
        after_agent_callback=_make_after_drafter_callback(),
    )

    return LoopAgent(
        name="implementation_loop",
        description="Generates and validates algorithm implementations.",
        max_iterations=max_retries,
        sub_agents=[drafter],
        before_agent_callback=_make_exit_condition(),
    )
