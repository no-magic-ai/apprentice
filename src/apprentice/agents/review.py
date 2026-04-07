"""Review Agent — ADK LoopAgent that validates all artifacts for consistency and schema."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools import exit_loop  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

from apprentice.validators.tools import consistency_validate, schema_validate

_REVIEWER_INSTRUCTION = """\
You are a quality reviewer for algorithm artifacts in the no-magic educational project.
Your job is to validate all generated artifacts for consistency and schema compliance.

The artifacts are available at paths stored in the session state:
- Implementation: {implementation_path}
- Instrumented: {instrumented_path}
- Manim scene: {manim_scene_path}
- Anki deck: {anki_deck_path}

Steps:
1. Build a JSON string mapping artifact types to their file paths from the session state.
   Format: '{{"implementation": "...", "instrumented": "...", "manim_scene": "...", "anki_deck": "..."}}'
2. Call consistency_validate with the artifacts JSON to check cross-artifact consistency.
3. Call schema_validate with the artifacts JSON to check schema compliance.

If ALL validators pass (both return passed=true), call exit_loop to finish successfully.
If any validator reports structural failures (severity="error"), compile detailed feedback
describing what needs to be fixed.
"""


def build_review_agent(
    model: LiteLlm,
    max_iterations: int = 2,
) -> LoopAgent:
    """Build an ADK LoopAgent for artifact review and validation.

    Runs consistency and schema compliance validators, exits on pass,
    compiles feedback on failure.

    Args:
        model: LiteLlm model instance.
        max_iterations: Maximum review rounds.

    Returns:
        A configured LoopAgent.
    """
    reviewer = LlmAgent(
        name="reviewer",
        model=model,
        instruction=_REVIEWER_INSTRUCTION,
        tools=[consistency_validate, schema_validate, exit_loop],
        output_key="review_verdict",
        description="Validates artifacts for consistency and schema compliance.",
    )

    return LoopAgent(
        name="review_loop",
        description="Iteratively reviews artifacts until all validators pass.",
        max_iterations=max_iterations,
        sub_agents=[reviewer],
    )
