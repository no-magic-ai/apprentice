"""Tests for ADK agent builder functions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.models.lite_llm import LiteLlm

from apprentice.agents.assessment import build_assessment_agent
from apprentice.agents.discovery import (
    build_discovery_agent,
    check_duplicate,
    load_catalog,
    validate_name,
)
from apprentice.agents.implementation import build_implementation_agent
from apprentice.agents.instrumentation import build_instrumentation_agent
from apprentice.agents.review import build_review_agent
from apprentice.agents.visualization import build_visualization_agent

if TYPE_CHECKING:
    from apprentice.core.artifacts import RunScope


def _model() -> LiteLlm:
    """Create a LiteLlm instance for testing agent construction."""
    return LiteLlm(model="anthropic/claude-sonnet-4-20250514")


class TestImplementationAgentBuilder:
    def test_returns_loop_agent(self, scope: RunScope) -> None:
        agent = build_implementation_agent(_model(), scope.work_root)
        assert isinstance(agent, LoopAgent)

    def test_name(self, scope: RunScope) -> None:
        agent = build_implementation_agent(_model(), scope.work_root)
        assert agent.name == "implementation_loop"

    def test_max_iterations(self, scope: RunScope) -> None:
        agent = build_implementation_agent(_model(), scope.work_root, max_retries=5)
        assert agent.max_iterations == 5

    def test_single_sub_agent(self, scope: RunScope) -> None:
        agent = build_implementation_agent(_model(), scope.work_root)
        assert len(agent.sub_agents) == 1

    def test_drafter_name(self, scope: RunScope) -> None:
        agent = build_implementation_agent(_model(), scope.work_root)
        assert agent.sub_agents[0].name == "drafter"


class TestInstrumentationAgentBuilder:
    def test_returns_llm_agent(self) -> None:
        agent = build_instrumentation_agent(_model())
        assert isinstance(agent, LlmAgent)

    def test_name(self) -> None:
        agent = build_instrumentation_agent(_model())
        assert agent.name == "instrumentation"

    def test_output_key(self) -> None:
        agent = build_instrumentation_agent(_model())
        assert agent.output_key == "instrumented_code"


class TestVisualizationAgentBuilder:
    def test_returns_llm_agent(self) -> None:
        agent = build_visualization_agent(_model())
        assert isinstance(agent, LlmAgent)

    def test_name(self) -> None:
        agent = build_visualization_agent(_model())
        assert agent.name == "visualization"

    def test_has_template_tool(self) -> None:
        agent = build_visualization_agent(_model())
        assert len(agent.tools) >= 1


class TestAssessmentAgentBuilder:
    def test_returns_llm_agent(self) -> None:
        agent = build_assessment_agent(_model())
        assert isinstance(agent, LlmAgent)

    def test_name(self) -> None:
        agent = build_assessment_agent(_model())
        assert agent.name == "assessment"

    def test_output_key(self) -> None:
        agent = build_assessment_agent(_model())
        assert agent.output_key == "anki_deck_content"


class TestDiscoveryAgentBuilder:
    def test_returns_llm_agent(self) -> None:
        agent = build_discovery_agent(_model())
        assert isinstance(agent, LlmAgent)

    def test_name(self) -> None:
        agent = build_discovery_agent(_model())
        assert agent.name == "discovery"

    def test_has_tools(self) -> None:
        agent = build_discovery_agent(_model())
        assert len(agent.tools) == 3


class TestDiscoveryTools:
    def test_load_catalog_returns_dict(self) -> None:
        result = load_catalog()
        assert isinstance(result, dict)
        assert "algorithms" in result
        assert "all_names" in result
        assert isinstance(result["algorithms"], list)

    def test_check_duplicate_known_name(self) -> None:
        result = check_duplicate("binary_search")
        assert result["is_duplicate"] is True

    def test_check_duplicate_unknown_name(self) -> None:
        result = check_duplicate("totally_unique_algo_xyz")
        assert result["is_duplicate"] is False

    def test_validate_name_valid(self) -> None:
        result = validate_name("merge_sort")
        assert result["valid"] is True
        assert result["normalized"] == "merge_sort"

    def test_validate_name_normalizes(self) -> None:
        result = validate_name("Merge-Sort")
        assert result["normalized"] == "merge_sort"
        assert result["valid"] is True


class TestReviewAgentBuilder:
    def test_returns_loop_agent(self, scope: RunScope) -> None:
        agent = build_review_agent(_model(), scope.work_root, scope.algorithm)
        assert isinstance(agent, LoopAgent)

    def test_name(self, scope: RunScope) -> None:
        agent = build_review_agent(_model(), scope.work_root, scope.algorithm)
        assert agent.name == "review_loop"

    def test_max_iterations(self, scope: RunScope) -> None:
        agent = build_review_agent(_model(), scope.work_root, scope.algorithm, max_iterations=3)
        assert agent.max_iterations == 3
