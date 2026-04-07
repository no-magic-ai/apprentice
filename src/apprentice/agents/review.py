"""Review Agent — ADK LoopAgent that validates all artifacts for consistency and schema."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools import exit_loop  # type: ignore[attr-defined]

from apprentice.validators.tools import consistency_validate, schema_validate

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_REVIEWER_INSTRUCTION = """\
You are a quality reviewer for algorithm artifacts in the no-magic educational project.
Your job is to validate all generated artifacts for consistency and schema compliance.

The artifacts are available in the conversation history:
- generated_code: the implementation source code
- instrumented_code: the instrumented source code
- manim_scene_code: the Manim visualization scene
- anki_deck_content: the Anki flashcard CSV

Steps:
1. Call save_artifacts to write all artifact content to temporary files.
   Pass the generated code, instrumented code, manim scene, and anki deck content
   from the conversation history.
2. Use the returned file paths to call consistency_validate with the artifacts JSON.
3. Call schema_validate with the same artifacts JSON.

If ALL validators pass (both return passed=true), call exit_loop to finish successfully.
If any validator reports structural failures (severity="error"), compile detailed feedback
describing what needs to be fixed.
"""


def save_artifacts(
    implementation_code: str = "",
    instrumented_code: str = "",
    manim_scene_code: str = "",
    anki_deck_content: str = "",
    algorithm_name: str = "algorithm",
) -> dict[str, Any]:
    """Save all artifact content to temporary files for validation.

    Args:
        implementation_code: Python source code for the algorithm.
        instrumented_code: Python source with trace hooks.
        manim_scene_code: Manim Scene Python source.
        anki_deck_content: CSV content for Anki flashcards.
        algorithm_name: Name used for temp file stems.

    Returns:
        Dict with file paths and a pre-built artifacts_json string
        ready to pass to consistency_validate and schema_validate.
    """
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

    return {
        "paths": paths,
        "artifacts_json": json.dumps(paths),
    }


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
        tools=[save_artifacts, consistency_validate, schema_validate, exit_loop],
        output_key="review_verdict",
        description="Validates artifacts for consistency and schema compliance.",
    )

    return LoopAgent(
        name="review_loop",
        description="Iteratively reviews artifacts until all validators pass.",
        max_iterations=max_iterations,
        sub_agents=[reviewer],
    )
