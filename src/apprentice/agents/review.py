"""Review Agent — ADK LoopAgent that validates all artifacts for consistency and schema."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools import exit_loop  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_REVIEWER_INSTRUCTION = """\
You are a quality reviewer for algorithm artifacts in the no-magic educational project.

Call validate_artifacts with the artifact content from the conversation history:
- implementation_code: the generated algorithm source code
- instrumented_code: the instrumented source code (if available)
- manim_scene_code: the Manim visualization scene (if available)
- anki_deck_content: the Anki flashcard CSV (if available)

The tool saves all artifacts to disk and runs consistency + schema validators.

If the tool returns all_passed=true, call exit_loop immediately.
If any validator failed, respond with a summary of the failures.
"""


def validate_artifacts(
    implementation_code: str = "",
    instrumented_code: str = "",
    manim_scene_code: str = "",
    anki_deck_content: str = "",
    algorithm_name: str = "algorithm",
) -> dict[str, Any]:
    """Save all artifacts and run consistency + schema validators in one call.

    Args:
        implementation_code: Python source code for the algorithm.
        instrumented_code: Python source with trace hooks.
        manim_scene_code: Manim Scene Python source.
        anki_deck_content: CSV content for Anki flashcards.
        algorithm_name: Name used for temp file stems.

    Returns:
        Dict with 'all_passed' bool and per-validator results.
    """
    from apprentice.validators.tools import consistency_validate, schema_validate

    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, str] = {}

    if implementation_code:
        impl_path = tmp_dir / f"{algorithm_name}.py"
        impl_path.write_text(implementation_code, encoding="utf-8")
        paths["implementation"] = str(impl_path)

    if instrumented_code:
        instr_path = tmp_dir / f"{algorithm_name}_instrumented.py"
        instr_path.write_text(instrumented_code, encoding="utf-8")
        paths["instrumented"] = str(instr_path)

    if manim_scene_code:
        manim_path = tmp_dir / f"{algorithm_name}_scene.py"
        manim_path.write_text(manim_scene_code, encoding="utf-8")
        paths["manim_scene"] = str(manim_path)

    if anki_deck_content:
        anki_path = tmp_dir / f"{algorithm_name}_cards.csv"
        anki_path.write_text(anki_deck_content, encoding="utf-8")
        paths["anki_deck"] = str(anki_path)

    artifacts_json = json.dumps(paths)
    consistency_result = consistency_validate(artifacts_json)
    schema_result = schema_validate(artifacts_json)

    all_passed = consistency_result["passed"] and schema_result["passed"]

    return {
        "all_passed": all_passed,
        "consistency": consistency_result,
        "schema": schema_result,
        "artifact_paths": paths,
    }


def build_review_agent(
    model: LiteLlm,
    max_iterations: int = 2,
) -> LoopAgent:
    """Build an ADK LoopAgent for artifact review and validation.

    Uses a single composite validate_artifacts tool that saves files and
    runs both consistency and schema validators in one call.

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
        tools=[validate_artifacts, exit_loop],
        output_key="review_verdict",
        description="Validates artifacts for consistency and schema compliance.",
    )

    return LoopAgent(
        name="review_loop",
        description="Iteratively reviews artifacts until all validators pass.",
        max_iterations=max_iterations,
        sub_agents=[reviewer],
    )
