"""Implementation Agent — ADK LoopAgent with LLM drafter + programmatic validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent

from apprentice.core.artifacts import write_role

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events import Event
    from google.adk.models.lite_llm import LiteLlm

_PASSED = "implementation_passed"

_DRAFTER_INSTRUCTION = """\
You are an expert algorithm implementer for the no-magic educational project.
Every file you produce is reviewed against the no-magic house style; the
commenting standard IS the primary merge criterion.

# Hard requirements

- Stdlib-only imports (`os`, `math`, `random`, `json`, `struct`, `urllib`,
  `collections`, `itertools`, `functools`, `string`, `hashlib`, `time`,
  `typing`, `dataclasses`). No third-party packages.
- `from __future__ import annotations` enabled.
- `random.seed(42)` is the first executable line after imports.
- Runs under 10 minutes on laptop CPU with zero CLI arguments.
- 4-space indentation, 100-char max line length.

# 7-point commenting standard (ALL required)

1. File thesis docstring — one sentence stating what the script PROVES.
2. Section headers — `# === SECTION NAME ===` between major phases
   (imports → constants → data → model → training → inference/demo).
3. Why comments — reasoning, not restatement.
4. Math-to-code mappings — show the equation; name variable correspondences.
5. Intuition comments — why the technique works.
6. Signpost comments — flag every simplification; note the production
   alternative.
7. No obvious comments — every comment adds information the code doesn't
   convey. Target 30-40% comment density.

# __main__ block

Minimum 3 assertions: normal case, edge case, stress case. Print a one-line
pass summary on success.

Return only the Python source code. No markdown fences, no prose.

# Feedback on your previous attempt (empty on the first attempt)

{validation_feedback?}
"""


def _run_validators(code: str, work_root: Path) -> dict[str, Any]:
    """Write code into the run's work root, run all validators and combine results."""
    from apprentice.validators.tools import correctness_validate, lint_validate, stdlib_check

    file_path = str(write_role(work_root, "implementation", code))

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


def _make_after_drafter_callback(work_root: Path) -> Any:
    """Create the drafter's after-agent callback that validates each draft.

    Runs after every drafter iteration: writes the draft into the run's work
    root, runs the validators and records whether it passed. A failed draft
    leaves feedback in `validation_feedback`, which the next iteration's
    instruction includes.
    """

    async def after_drafter(callback_context: Any) -> Any:
        state = callback_context.state
        code = state.get("generated_code", "")

        if not code:
            state[_PASSED] = False
            state["validation_feedback"] = (
                "No code was generated. Write complete Python source code."
            )
            return None

        result = _run_validators(code, work_root)

        state[_PASSED] = result["all_passed"]
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


class ValidationCheckpoint(BaseAgent):
    """Ends the implementation loop once the latest draft passed validation.

    Runs after the drafter in every iteration and escalates out of the
    `LoopAgent` when the draft passed, so no further drafts are requested.
    A failed draft continues to the next iteration until the loop's maximum.
    """

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        from google.adk.events import Event, EventActions

        if ctx.session.state.get(_PASSED) is True:
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                branch=getattr(ctx, "branch", None),
                actions=EventActions(escalate=True),
            )


def build_implementation_agent(
    model: LiteLlm,
    work_root: Path,
    max_attempts: int,
) -> LoopAgent:
    """Build an ADK LoopAgent for algorithm implementation with programmatic validation.

    Each iteration is one drafter call, the drafter's validation callback
    and the checkpoint: a passing draft ends the loop, a failing one feeds
    its issues into the next iteration. `max_attempts` is the total number of
    drafts including the first (`agents.max_implementation_retries`); when
    every draft failed the loop ends and the blocking gates fail the run.

    Args:
        model: Metered model of the implementation role.
        work_root: The run's exclusive work root the drafter output is validated in.
        max_attempts: Total drafter iterations, including the first.

    Returns:
        A configured LoopAgent ready for pipeline integration.
    """
    drafter = LlmAgent(
        name="drafter",
        model=model,
        instruction=_DRAFTER_INSTRUCTION,
        output_key="generated_code",
        after_agent_callback=_make_after_drafter_callback(work_root),
    )

    return LoopAgent(
        name="implementation_loop",
        description="Generates and validates algorithm implementations.",
        max_iterations=max_attempts,
        sub_agents=[drafter, ValidationCheckpoint(name="implementation_checkpoint")],
    )
