"""Tests for the Implementation Agent ADK LoopAgent builder."""

from __future__ import annotations

from google.adk.agents import LoopAgent
from google.adk.models.lite_llm import LiteLlm

from apprentice.agents.implementation import build_implementation_agent


class TestImplementationAgent:
    def test_returns_loop_agent(self) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model)
        assert isinstance(agent, LoopAgent)

    def test_name(self) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model)
        assert agent.name == "implementation_loop"

    def test_max_iterations_default(self) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model)
        assert agent.max_iterations == 3

    def test_max_iterations_custom(self) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model, max_retries=5)
        assert agent.max_iterations == 5

    def test_has_drafter_and_reviewer(self) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model)
        names = [a.name for a in agent.sub_agents]
        assert "drafter" in names
        assert "self_reviewer" in names

    def test_reviewer_has_tools(self) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model)
        reviewer = next(a for a in agent.sub_agents if a.name == "self_reviewer")
        assert len(reviewer.tools) == 2

    def test_drafter_output_key(self) -> None:
        model = LiteLlm(model="anthropic/claude-sonnet-4-20250514")
        agent = build_implementation_agent(model)
        drafter = next(a for a in agent.sub_agents if a.name == "drafter")
        assert drafter.output_key == "generated_code"
