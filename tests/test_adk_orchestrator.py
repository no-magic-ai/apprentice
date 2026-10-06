"""Tests for the ADK orchestrator pipeline builder."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import ParallelAgent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm

from apprentice.core.config import (
    AgentBudgetConfig,
    AgentsConfig,
    ApprenticeConfig,
    BudgetConfig,
    CircuitBreakerConfig,
    CycleBudgetConfig,
    GatesConfig,
    GlobalBudgetConfig,
    ObservabilityConfig,
    ProviderConfig,
    RateLimitsConfig,
    StageBudgetConfig,
    TemplatesConfig,
)
from apprentice.core.gate_agent import GateAgent
from apprentice.core.orchestrator import build_discovery_pipeline, build_pipeline

if TYPE_CHECKING:
    from apprentice.core.artifacts import RunScope


def _model() -> LiteLlm:
    return LiteLlm(model="anthropic/claude-sonnet-4-20250514")


def _config() -> ApprenticeConfig:
    return ApprenticeConfig(
        budget=BudgetConfig(
            global_budget=GlobalBudgetConfig(
                monthly_token_ceiling=2_000_000,
                monthly_cost_ceiling_usd=50.0,
            ),
            cycle=CycleBudgetConfig(
                max_tokens_per_cycle=100_000,
                max_cost_per_cycle_usd=5.0,
                max_algorithms_per_cycle=3,
            ),
            stage=StageBudgetConfig(max_tokens_per_stage=20_000),
            agent=AgentBudgetConfig(
                max_tokens_per_agent_call=20_000,
                implementation_budget_pct=40,
                tool_agent_budget_pct=15,
                review_budget_pct=15,
            ),
        ),
        rate_limits=RateLimitsConfig(
            max_prs_per_day=2,
            max_prs_per_week=5,
            max_concurrent_items=1,
            cooldown_hours=4,
            max_files_per_pr=10,
            max_lines_per_pr=2000,
        ),
        gates=GatesConfig(
            max_lint_retries=2,
            max_correctness_retries=1,
            max_review_rounds=2,
        ),
        agents=AgentsConfig(
            max_implementation_retries=3,
            max_review_rounds=2,
            max_tool_agent_retries=1,
        ),
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=3,
            half_open_probe_after_minutes=60,
            max_open_cycles_before_manual_reset=3,
        ),
        provider=ProviderConfig(
            backend="anthropic",
            model="anthropic/claude-sonnet-4-20250514",
            fallback_model="anthropic/claude-haiku-4-5-20251001",
            local_api_base="",
        ),
        observability=ObservabilityConfig(
            log_level="INFO",
            log_format="json",
            log_path="${HOME}/.apprentice/logs",
            metrics_enabled=True,
            alert_on_circuit_open=True,
            alert_webhook="",
        ),
        templates=TemplatesConfig(version="1.0.0", base_path="config/templates"),
    )


class TestBuildPipeline:
    def test_returns_sequential_agent(self, scope: RunScope) -> None:
        pipeline = build_pipeline(_model(), _config(), scope)
        assert isinstance(pipeline, SequentialAgent)

    def test_pipeline_name(self, scope: RunScope) -> None:
        pipeline = build_pipeline(_model(), _config(), scope)
        assert pipeline.name == "apprentice_pipeline"

    def test_pipeline_only_generates(self, scope: RunScope) -> None:
        pipeline = build_pipeline(_model(), _config(), scope)
        names = [a.name for a in pipeline.sub_agents]
        assert names[-1] == "review_loop"
        assert not any("packaging" in name or "review_after" in name for name in names)

    def test_gate_agents_inserted(self, scope: RunScope) -> None:
        pipeline = build_pipeline(_model(), _config(), scope)
        gate_agents = [a for a in pipeline.sub_agents if isinstance(a, GateAgent)]
        gate_names = {g.gate.name for g in gate_agents}
        assert gate_names == {"correctness", "lint", "consistency", "schema_compliance"}

    def test_parallel_agent_in_pipeline(self, scope: RunScope) -> None:
        pipeline = build_pipeline(_model(), _config(), scope)
        parallel_agents = [a for a in pipeline.sub_agents if isinstance(a, ParallelAgent)]
        assert len(parallel_agents) == 1
        assert parallel_agents[0].name == "artifact_generation"

    def test_parallel_has_three_sub_agents(self, scope: RunScope) -> None:
        pipeline = build_pipeline(_model(), _config(), scope)
        parallel = next(a for a in pipeline.sub_agents if isinstance(a, ParallelAgent))
        assert len(parallel.sub_agents) == 3

    def test_has_callbacks(self, scope: RunScope) -> None:
        pipeline = build_pipeline(_model(), _config(), scope)
        assert pipeline.before_agent_callback is not None
        assert pipeline.after_agent_callback is not None


class TestBuildDiscoveryPipeline:
    def test_returns_agent(self) -> None:
        agent = build_discovery_pipeline(_model())
        assert agent is not None
        assert agent.name == "discovery"
