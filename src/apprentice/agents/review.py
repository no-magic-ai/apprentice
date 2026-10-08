"""Review Agent — programmatic validation of every generated artifact.

No model is involved: the consistency and schema validators run once as
pure Python over the run's artifacts and record `review_verdict`.
"""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 — pydantic needs it at runtime
from typing import TYPE_CHECKING, Any

from google.adk.agents import BaseAgent

from apprentice.core.artifacts import write_state_roles

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events import Event


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


class ProgrammaticReviewAgent(BaseAgent):
    """Validates all artifacts in the run's work root and records the verdict."""

    work_root: Path
    algorithm_name: str

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        from google.adk.events import Event, EventActions
        from google.genai import types

        result = _validate_all_artifacts(
            dict(ctx.session.state), self.work_root, self.algorithm_name
        )
        if result["all_passed"]:
            verdict = "passed"
            message = "All artifacts validated successfully."
        else:
            verdict = "failed: " + "; ".join(result["failures"])
            message = "Review: " + "; ".join(result["failures"])
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=getattr(ctx, "branch", None),
            content=types.Content(role="model", parts=[types.Part(text=message)]),
            actions=EventActions(state_delta={"review_verdict": verdict}),
        )


def build_review_agent(work_root: Path, algorithm_name: str) -> ProgrammaticReviewAgent:
    """Build the review stage: one programmatic validation of every artifact.

    Args:
        work_root: The run's exclusive work root artifacts are validated in.
        algorithm_name: The run's algorithm, checked for cross-artifact consistency.

    Returns:
        The review agent; it makes no model call.
    """
    return ProgrammaticReviewAgent(
        name="reviewer",
        description="Validates all artifacts for consistency and schema compliance.",
        work_root=work_root,
        algorithm_name=algorithm_name,
    )
