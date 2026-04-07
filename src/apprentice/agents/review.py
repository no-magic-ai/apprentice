"""Review Agent — programmatic artifact validation via ADK callbacks.

No LLM is used for review — consistency and schema validators run
as pure Python in an after_agent_callback. The LoopAgent iterates
only if validation fails and there's a preceding agent to fix artifacts.
Since artifact agents don't retry, this effectively runs once.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent, LoopAgent

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm


def _validate_all_artifacts(state: dict[str, Any]) -> dict[str, Any]:
    """Run consistency and schema validators on all artifacts in session state."""
    from apprentice.validators.tools import consistency_validate, schema_validate

    algorithm_name = state.get("algorithm_name", "algorithm")
    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, str] = {}

    impl_code = state.get("generated_code", "")
    if impl_code:
        p = tmp_dir / f"{algorithm_name}.py"
        p.write_text(impl_code, encoding="utf-8")
        paths["implementation"] = str(p)

    instr_code = state.get("instrumented_code", "")
    if instr_code:
        p = tmp_dir / f"{algorithm_name}_instrumented.py"
        p.write_text(instr_code, encoding="utf-8")
        paths["instrumented"] = str(p)

    manim_code = state.get("manim_scene_code", "")
    if manim_code:
        p = tmp_dir / f"{algorithm_name}_scene.py"
        p.write_text(manim_code, encoding="utf-8")
        paths["manim_scene"] = str(p)

    anki_content = state.get("anki_deck_content", "")
    if anki_content:
        p = tmp_dir / f"{algorithm_name}_cards.csv"
        p.write_text(anki_content, encoding="utf-8")
        paths["anki_deck"] = str(p)

    if not paths:
        return {
            "all_passed": False,
            "failures": ["No artifacts found in session state"],
            "artifact_paths": {},
        }

    artifacts_json = json.dumps(paths)
    consistency = consistency_validate(artifacts_json)
    schema = schema_validate(artifacts_json)

    all_passed = consistency["passed"] and schema["passed"]

    failures: list[str] = []
    for result in (consistency, schema):
        for issue in result.get("issues", []):
            if issue.get("severity") == "error":
                failures.append(f"{issue.get('artifact', '')}: {issue.get('message', '')}")

    return {"all_passed": all_passed, "failures": failures, "artifact_paths": paths}


def build_review_agent(
    model: LiteLlm,
    max_iterations: int = 2,
) -> LoopAgent:
    """Build a review stage that validates artifacts programmatically.

    Uses a no-op LlmAgent as a placeholder inside a LoopAgent.
    The before_agent_callback runs validators and either exits (pass)
    or continues with feedback (fail). No LLM calls are made.

    Args:
        model: LiteLlm model instance (used for the placeholder agent).
        max_iterations: Maximum review rounds.

    Returns:
        A configured LoopAgent.
    """

    async def review_callback(callback_context: Any) -> Any:
        from google.genai import types

        state = callback_context.state
        result = _validate_all_artifacts(state)

        if result["all_passed"]:
            state["review_verdict"] = "passed"
            return types.Content(
                role="model",
                parts=[types.Part(text="All artifacts validated successfully.")],
            )

        state["review_verdict"] = "failed: " + "; ".join(result["failures"])
        return types.Content(
            role="model",
            parts=[types.Part(text="Review: " + "; ".join(result["failures"]))],
        )

    placeholder = LlmAgent(
        name="reviewer",
        model=model,
        instruction="Artifacts are validated automatically.",
        output_key="review_verdict",
    )

    return LoopAgent(
        name="review_loop",
        description="Validates all artifacts for consistency and schema compliance.",
        max_iterations=max_iterations,
        sub_agents=[placeholder],
        before_agent_callback=review_callback,
    )
