"""Implementation Agent — ADK LoopAgent with drafter + self-reviewer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools import exit_loop  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

from apprentice.validators.tools import correctness_validate, lint_validate, stdlib_check

_DRAFTER_INSTRUCTION = """\
You are an expert algorithm implementer for the no-magic educational project.
You write clean, well-documented Python implementations with:
- Type hints on all function signatures
- Google-style docstrings with Args, Returns, Complexity sections
- Zero external dependencies (stdlib only)
- Inline comments explaining key algorithmic decisions
- Reference test cases in an `if __name__ == "__main__":` block

Generate the algorithm implementation based on the task description.
If previous feedback is available in {{review_feedback}}, fix ALL listed issues.

Write the complete Python source code. Do NOT use markdown fences.
Write ONLY the Python source code, nothing else.

Save the generated code by writing it to the session state under the key 'generated_code'.
"""

_REVIEWER_INSTRUCTION = """\
You are a code reviewer for algorithm implementations.
Your job is to validate generated code using the available tools.

The generated code is available at the file path in {{implementation_path}}.

Steps:
1. Run stdlib_check on the implementation file to verify no third-party imports.
2. Run lint_validate on the implementation file to check style and structure.
3. Run correctness_validate on the implementation file to verify it executes cleanly.

If ALL validators pass, call the exit_loop tool to finish successfully.
If any validator fails, summarize the issues clearly for the drafter to fix.
Include the specific error messages and suggestions in your feedback.
"""


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
        tools=[lint_validate, correctness_validate, stdlib_check, exit_loop],
        output_key="review_feedback",
    )

    return LoopAgent(
        name="implementation_loop",
        description="Iteratively generates and validates algorithm implementations.",
        max_iterations=max_retries,
        sub_agents=[drafter, reviewer],
    )
