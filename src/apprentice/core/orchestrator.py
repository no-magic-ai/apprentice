"""ADK Orchestrator — builds the full agent pipeline using ADK primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.adk.agents import ParallelAgent, SequentialAgent

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

from apprentice.agents.assessment import build_assessment_agent
from apprentice.agents.implementation import build_implementation_agent
from apprentice.agents.instrumentation import build_instrumentation_agent
from apprentice.agents.packaging import build_packaging_agent
from apprentice.agents.review import build_review_agent
from apprentice.agents.visualization import build_visualization_agent
from apprentice.core.artifacts import ROLE_FILENAMES, tier_directory, validate_algorithm_name
from apprentice.core.budget import (
    BudgetTracker,
    make_after_agent_callback,
    make_before_agent_callback,
    wire_agent_callbacks,
)
from apprentice.core.gate_agent import GateAgent
from apprentice.gates.consistency import ConsistencyGate
from apprentice.gates.correctness import CorrectnessGate
from apprentice.gates.lint import LintGate
from apprentice.gates.review import ReviewGate
from apprentice.gates.schema_compliance import SchemaComplianceGate

if TYPE_CHECKING:
    from apprentice.core.artifacts import RunScope
    from apprentice.core.config import ApprenticeConfig


def build_pipeline(
    model: LiteLlm,
    config: ApprenticeConfig,
    scope: RunScope,
    include_packaging: bool = False,
    approval: dict[str, Any] | None = None,
) -> SequentialAgent:
    """Build the full ADK pipeline as a SequentialAgent with gates between stages.

    Stage/gate ordering:
        1. implementation_loop
           → correctness gate (blocking)
           → lint gate (blocking)
        2. artifact_generation (instrumentation | visualization | assessment)
           → consistency gate (blocking)
           → schema_compliance gate (blocking)
        3. review_loop
        4. [submit only] review gate (human approval hard stop, blocking)
        5. [submit only] packaging

    Every sub-agent (and gate) gets the same shared `BudgetTracker` so the
    run record reports per-stage token/cost rows (fixes #13 collapse into
    a single `apprentice_pipeline` row) plus ordered gate verdicts.

    Args:
        model: LiteLlm model instance for all agents.
        config: Full apprentice configuration.
        scope: Identity and exclusive work root of the run; every gate and
            validation callback writes and evaluates artifacts only there.
        include_packaging: Whether to include the packaging agent
            (True for `submit`, False for `build`).
        approval: Human-review approval payload loaded from the run record.
            Required when `include_packaging=True`; otherwise the review gate
            fails and packaging never executes.

    Returns:
        A configured SequentialAgent representing the full pipeline.
    """
    tracker = BudgetTracker(
        total_tokens=config.budget.cycle.max_tokens_per_cycle,
        total_usd=config.budget.cycle.max_cost_per_cycle_usd,
    )

    implementation_agent = build_implementation_agent(
        model,
        scope.work_root,
        max_retries=config.agents.max_implementation_retries,
    )
    artifact_parallel = ParallelAgent(
        name="artifact_generation",
        description="Generates instrumentation, visualization, and assessment artifacts concurrently.",
        sub_agents=[
            build_instrumentation_agent(model),
            build_visualization_agent(model),
            build_assessment_agent(model),
        ],
    )
    review_agent = build_review_agent(
        model,
        scope.work_root,
        scope.algorithm,
        max_iterations=config.agents.max_review_rounds,
    )

    sub_agents: list[Any] = [
        implementation_agent,
        GateAgent.after(CorrectnessGate(), "implementation", scope, tracker=tracker),
        GateAgent.after(LintGate(), "implementation", scope, tracker=tracker),
        artifact_parallel,
        GateAgent.after(ConsistencyGate(), "artifact_generation", scope, tracker=tracker),
        GateAgent.after(SchemaComplianceGate(), "artifact_generation", scope, tracker=tracker),
        review_agent,
    ]

    if include_packaging:
        sub_agents.append(
            GateAgent.after(ReviewGate(approval=approval), "review", scope, tracker=tracker),
        )
        sub_agents.append(build_packaging_agent(model))

    pipeline = SequentialAgent(
        name="apprentice_pipeline",
        description="Full apprentice pipeline: implement → gate → generate artifacts → gate → review → package.",
        sub_agents=sub_agents,
        before_agent_callback=make_before_agent_callback(tracker),
        after_agent_callback=make_after_agent_callback(
            tracker, model_name=getattr(model, "model", "")
        ),
    )

    model_name = getattr(model, "model", "")
    for sub in sub_agents:
        wire_agent_callbacks(sub, tracker, model_name)

    if include_packaging:
        # The old publisher's instruction names the regenerated role paths and
        # the destination name/tier. Seed them from the fresh regeneration root
        # (roles that were not regenerated stay empty) after the budget wiring
        # above, which replaces each agent's before_agent_callback.
        publisher = sub_agents[-1]
        name = validate_algorithm_name(scope.algorithm)
        tier_dir = tier_directory(scope.tier)
        state_keys = {
            "implementation_path": "implementation",
            "instrumented_path": "instrumented",
            "manim_scene_path": "manim_scene",
            "anki_deck_path": "anki_deck",
        }

        async def seed_publisher_context(callback_context: Any) -> None:
            for key, role in state_keys.items():
                path = scope.work_root / ROLE_FILENAMES[role]
                callback_context.state[key] = str(path) if path.is_file() else ""
            callback_context.state["name"] = name
            callback_context.state["tier_dir"] = tier_dir

        publisher.before_agent_callback = [
            publisher.before_agent_callback,
            seed_publisher_context,
        ]

    return pipeline


def build_discovery_pipeline(model: LiteLlm) -> Any:
    """Build a standalone discovery agent for the suggest command."""
    from apprentice.agents.discovery import build_discovery_agent

    return build_discovery_agent(model)


def get_budget_tracker_from_pipeline(pipeline: SequentialAgent) -> BudgetTracker | None:
    """Extract the BudgetTracker from a pipeline's callbacks."""
    cb = pipeline.before_agent_callback
    if cb is not None and hasattr(cb, "__closure__") and cb.__closure__:
        for cell in cb.__closure__:
            contents = cell.cell_contents
            if isinstance(contents, BudgetTracker):
                return contents
    return None
