"""ADK Orchestrator — builds the full agent pipeline using ADK primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.adk.agents import ParallelAgent, SequentialAgent

from apprentice.agents.assessment import build_assessment_agent
from apprentice.agents.implementation import build_implementation_agent
from apprentice.agents.instrumentation import build_instrumentation_agent
from apprentice.agents.review import build_review_agent
from apprentice.agents.visualization import build_visualization_agent
from apprentice.core.gate_agent import GateAgent
from apprentice.gates.consistency import ConsistencyGate
from apprentice.gates.correctness import CorrectnessGate
from apprentice.gates.lint import LintGate
from apprentice.gates.schema_compliance import SchemaComplianceGate

if TYPE_CHECKING:
    from apprentice.controls.authority import Cycle
    from apprentice.core.artifacts import RunScope
    from apprentice.core.budget import BudgetTracker
    from apprentice.providers.factory import ModelRoute

IMPLEMENTATION_STAGE = "implementation"
ARTIFACT_STAGE = "artifact_generation"
DISCOVERY_STAGE = "discovery"


def build_pipeline(
    route: ModelRoute,
    cycle: Cycle,
    scope: RunScope,
    tracker: BudgetTracker,
) -> SequentialAgent:
    """Build the full ADK pipeline as a SequentialAgent with gates between stages.

    Stage/gate ordering:
        1. implementation_loop
           → correctness gate (blocking)
           → lint gate (blocking)
        2. artifact_generation (instrumentation | visualization | assessment)
           → consistency gate (blocking)
           → schema_compliance gate (blocking)
        3. reviewer (programmatic, no model call)

    The pipeline only generates; `submit` never runs it. Packaging promotes
    the approved sealed bundle deterministically (`agents/packaging.py`).

    Every model is the route bound to `cycle` with its own stage and role, so
    each request is reserved and settled in the installation ledger before
    and after it is sent. Gates record their verdicts in `tracker`.

    Args:
        route: The resolved, qualified model route.
        cycle: The admitted cycle every model call is metered against; its
            policy fixes the implementation attempts.
        scope: Identity and exclusive work root of the run; every gate and
            validation callback writes and evaluates artifacts only there.
        tracker: Collects the gate verdicts of this run.

    Returns:
        A configured SequentialAgent representing the full pipeline.
    """
    implementation_agent = build_implementation_agent(
        route.model(cycle, IMPLEMENTATION_STAGE, "implementation"),
        scope.work_root,
        max_attempts=cycle.policy.max_implementation_retries,
    )
    artifact_parallel = ParallelAgent(
        name="artifact_generation",
        description="Generates instrumentation, visualization, and assessment artifacts concurrently.",
        sub_agents=[
            build_instrumentation_agent(route.model(cycle, ARTIFACT_STAGE, "instrumentation")),
            build_visualization_agent(route.model(cycle, ARTIFACT_STAGE, "visualization")),
            build_assessment_agent(route.model(cycle, ARTIFACT_STAGE, "assessment")),
        ],
    )

    sub_agents: list[Any] = [
        implementation_agent,
        GateAgent.after(CorrectnessGate(), "implementation", scope, tracker=tracker),
        GateAgent.after(LintGate(), "implementation", scope, tracker=tracker),
        artifact_parallel,
        GateAgent.after(ConsistencyGate(), "artifact_generation", scope, tracker=tracker),
        GateAgent.after(SchemaComplianceGate(), "artifact_generation", scope, tracker=tracker),
        build_review_agent(scope.work_root, scope.algorithm),
    ]

    return SequentialAgent(
        name="apprentice_pipeline",
        description="Full apprentice pipeline: implement → gate → generate artifacts → gate → review.",
        sub_agents=sub_agents,
    )


def build_discovery_pipeline(route: ModelRoute, cycle: Cycle) -> Any:
    """Build a standalone discovery agent for the suggest command, metered against `cycle`."""
    from apprentice.agents.discovery import build_discovery_agent

    return build_discovery_agent(route.model(cycle, DISCOVERY_STAGE, "discovery"))
