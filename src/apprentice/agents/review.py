"""Review Agent — programmatic artifact validation via ADK callbacks.

No LLM is used for review — consistency and schema validators run
as pure Python in an after_agent_callback. The LoopAgent iterates
only if validation fails and there's a preceding agent to fix artifacts.
Since artifact agents don't retry, this effectively runs once.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent, LoopAgent

from apprentice.core.artifacts import write_state_roles

if TYPE_CHECKING:
    from pathlib import Path

    from google.adk.models.lite_llm import LiteLlm


def _validate_all_artifacts(
    state: dict[str, Any], work_root: Path, algorithm_name: str
) -> dict[str, Any]:
    """Write session-state artifacts into the run's work root and validate them all."""
    from apprentice.validators.tools import consistency_validate, schema_validate

    paths = {role: str(path) for role, path in write_state_roles(work_root, state).items()}

    if not paths:
        return {
            "all_passed": False,
            "failures": ["No artifacts found in session state"],
            "artifact_paths": {},
        }

    artifacts_json = json.dumps(paths)
    consistency = consistency_validate(artifacts_json, algorithm_name)
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
    work_root: Path,
    algorithm_name: str,
    max_iterations: int = 2,
) -> LoopAgent:
    """Build a review stage that validates artifacts programmatically.

    Uses a no-op LlmAgent as a placeholder inside a LoopAgent.
    The before_agent_callback runs validators and either exits (pass)
    or continues with feedback (fail). No LLM calls are made.

    Args:
        model: LiteLlm model instance (used for the placeholder agent).
        work_root: The run's exclusive work root artifacts are validated in.
        algorithm_name: The run's algorithm, checked for cross-artifact consistency.
        max_iterations: Maximum review rounds.

    Returns:
        A configured LoopAgent.
    """

    async def review_callback(callback_context: Any) -> Any:
        from google.genai import types

        state = callback_context.state
        result = _validate_all_artifacts(state, work_root, algorithm_name)

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
