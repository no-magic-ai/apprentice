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
from apprentice.core.budget import (
    BudgetTracker,
    make_after_agent_callback,
    make_before_agent_callback,
)

if TYPE_CHECKING:
    from apprentice.core.config import ApprenticeConfig


def build_pipeline(
    model: LiteLlm,
    config: ApprenticeConfig,
    include_packaging: bool = False,
) -> SequentialAgent:
    """Build the full ADK pipeline as a SequentialAgent.

    Pipeline structure:
        1. Implementation (LoopAgent: drafter → self_reviewer, max 3 iterations)
        2. Parallel artifact generation:
           - Instrumentation (LlmAgent)
           - Visualization (LlmAgent)
           - Assessment (LlmAgent)
        3. Review (LoopAgent: reviewer, max 2 iterations)
        4. Packaging (LlmAgent, only when include_packaging=True)

    Args:
        model: LiteLlm model instance for all agents.
        config: Full apprentice configuration.
        include_packaging: Whether to include the packaging agent
            (True for 'submit', False for 'build').

    Returns:
        A configured SequentialAgent representing the full pipeline.
    """
    tracker = BudgetTracker(
        total_tokens=config.budget.cycle.max_tokens_per_cycle,
        total_usd=config.budget.cycle.max_cost_per_cycle_usd,
    )

    sub_agents: list[Any] = [
        build_implementation_agent(
            model,
            max_retries=config.agents.max_implementation_retries,
        ),
        ParallelAgent(
            name="artifact_generation",
            description="Generates instrumentation, visualization, and assessment artifacts concurrently.",
            sub_agents=[
                build_instrumentation_agent(model),
                build_visualization_agent(model),
                build_assessment_agent(model),
            ],
        ),
        build_review_agent(
            model,
            max_iterations=config.agents.max_review_rounds,
        ),
    ]

    if include_packaging:
        sub_agents.append(build_packaging_agent(model))

    return SequentialAgent(
        name="apprentice_pipeline",
        description="Full apprentice pipeline: implement → generate artifacts → review → package.",
        sub_agents=sub_agents,
        before_agent_callback=make_before_agent_callback(tracker),
        after_agent_callback=make_after_agent_callback(tracker),
    )


def build_discovery_pipeline(model: LiteLlm) -> Any:
    """Build a standalone discovery agent for the suggest command.

    Args:
        model: LiteLlm model instance.

    Returns:
        A configured discovery LlmAgent.
    """
    from apprentice.agents.discovery import build_discovery_agent

    return build_discovery_agent(model)


def get_budget_tracker_from_pipeline(pipeline: SequentialAgent) -> BudgetTracker | None:
    """Extract the BudgetTracker from a pipeline's callbacks.

    Args:
        pipeline: The pipeline SequentialAgent.

    Returns:
        The BudgetTracker if found, None otherwise.
    """
    cb = pipeline.before_agent_callback
    if cb is not None and hasattr(cb, "__closure__") and cb.__closure__:
        for cell in cb.__closure__:
            contents = cell.cell_contents
            if isinstance(contents, BudgetTracker):
                return contents
    return None
