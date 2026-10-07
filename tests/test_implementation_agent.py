"""Tests for the Implementation Agent ADK LoopAgent builder."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import LoopAgent
from google.adk.models.lite_llm import LiteLlm

from apprentice.agents.implementation import build_implementation_agent

if TYPE_CHECKING:
    from apprentice.core.artifacts import RunScope


class TestImplementationAgent:
    def test_returns_loop_agent(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root)
        assert isinstance(agent, LoopAgent)

    def test_name(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root)
        assert agent.name == "implementation_loop"

    def test_max_iterations_default(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root)
        assert agent.max_iterations == 3

    def test_max_iterations_custom(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root, max_retries=5)
        assert agent.max_iterations == 5

    def test_has_drafter(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root)
        names = [a.name for a in agent.sub_agents]
        assert "drafter" in names

    def test_single_sub_agent(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root)
        assert len(agent.sub_agents) == 1

    def test_drafter_output_key(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root)
        drafter = agent.sub_agents[0]
        assert drafter.output_key == "generated_code"

    def test_has_after_agent_callback(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root)
        drafter = agent.sub_agents[0]
        assert drafter.after_agent_callback is not None

    def test_has_before_agent_callback(self, scope: RunScope) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, scope.work_root)
        assert agent.before_agent_callback is not None
