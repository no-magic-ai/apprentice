"""ADK budget callbacks — track and enforce token/cost budgets per agent."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from apprentice.core.observability import get_logger

_logger = get_logger(__name__)


@dataclass
class BudgetTracker:
    """Tracks token and cost budget across the ADK pipeline.

    Attributes:
        total_tokens: Maximum tokens allowed for the entire pipeline run.
        total_usd: Maximum USD allowed for the entire pipeline run.
        tokens_used: Running total of tokens consumed.
        cost_usd: Running total of USD spent.
        per_agent: Per-agent token and cost breakdown, one entry per stage
            name (e.g. `drafter`, `instrumentation`, `visualization`,
            `assessment`, `reviewer`, `packaging`, plus gate agents).
        gate_verdicts: Ordered list of gate results appended in execution order.
    """

    total_tokens: int
    total_usd: float
    tokens_used: int = 0
    cost_usd: float = 0.0
    per_agent: dict[str, dict[str, Any]] = field(default_factory=dict)
    gate_verdicts: list[dict[str, Any]] = field(default_factory=list)
    _agent_start_times: dict[str, float] = field(default_factory=dict)

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.total_tokens - self.tokens_used)

    @property
    def usd_remaining(self) -> float:
        return max(0.0, self.total_usd - self.cost_usd)

    @property
    def is_exhausted(self) -> bool:
        return self.tokens_used >= self.total_tokens or self.cost_usd >= self.total_usd

    def record_agent_start(self, agent_name: str) -> None:
        """Record the start time for an agent execution."""
        self._agent_start_times[agent_name] = time.monotonic()
        if agent_name not in self.per_agent:
            self.per_agent[agent_name] = {
                "tokens_used": 0,
                "cost_usd": 0.0,
                "calls": 0,
                "duration_seconds": 0.0,
            }

    def record_agent_completion(
        self,
        agent_name: str,
        tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Record completion of an agent execution with token/cost data."""
        self.tokens_used += tokens
        self.cost_usd += cost

        start = self._agent_start_times.pop(agent_name, time.monotonic())
        duration = time.monotonic() - start

        entry = self.per_agent.setdefault(
            agent_name,
            {"tokens_used": 0, "cost_usd": 0.0, "calls": 0, "duration_seconds": 0.0},
        )
        entry["tokens_used"] += tokens
        entry["cost_usd"] += cost
        entry["calls"] += 1
        entry["duration_seconds"] += duration

    def record_gate_verdict(
        self,
        gate_name: str,
        after_stage: str,
        verdict: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        """Append a gate verdict in execution order."""
        self.gate_verdicts.append(
            {
                "gate_name": gate_name,
                "after_stage": after_stage,
                "verdict": verdict,
                "diagnostics": diagnostics or {},
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "total_usd": self.total_usd,
            "tokens_used": self.tokens_used,
            "cost_usd": round(self.cost_usd, 6),
            "tokens_remaining": self.tokens_remaining,
            "usd_remaining": round(self.usd_remaining, 6),
            "per_agent": self.per_agent,
            "gate_verdicts": self.gate_verdicts,
        }


_OUTPUT_KEY_BY_AGENT: dict[str, str] = {
    "drafter": "generated_code",
    "instrumentation": "instrumented_code",
    "visualization": "manim_scene_code",
    "assessment": "anki_deck_content",
    "reviewer": "review_verdict",
    "review_loop": "review_verdict",
    "packaging": "pr_urls",
    "discovery": "discovery_candidates",
}


def make_before_agent_callback(
    tracker: BudgetTracker,
) -> Any:
    """Create an ADK before_agent_callback that checks budget before dispatch."""

    async def before_agent_budget_check(callback_context: Any) -> Any:
        agent_name = getattr(callback_context, "agent_name", "unknown")
        tracker.record_agent_start(agent_name)

        if tracker.is_exhausted:
            _logger.warning(
                "budget_exhausted_before_agent",
                extra={
                    "agent_name": agent_name,
                    "tokens_used": tracker.tokens_used,
                    "cost_usd": tracker.cost_usd,
                },
            )

        _logger.info(
            "agent_dispatched",
            extra={
                "agent_name": agent_name,
                "tokens_remaining": tracker.tokens_remaining,
                "usd_remaining": round(tracker.usd_remaining, 6),
            },
        )
        return None

    return before_agent_budget_check


def make_after_agent_callback(
    tracker: BudgetTracker,
    model_name: str = "",
) -> Any:
    """Create an ADK after_agent_callback that tracks cost after completion.

    Attributes the most recent unattributed output key (per `_OUTPUT_KEY_BY_AGENT`)
    to the agent that just finished, so each stage gets its own token/cost row
    rather than everything collapsing into the pipeline root.
    """
    _seen_keys: set[str] = set()

    async def after_agent_track_cost(callback_context: Any) -> Any:
        from apprentice.core.tokens import estimate_cost

        agent_name = getattr(callback_context, "agent_name", "unknown")
        state = getattr(callback_context, "state", {})

        tokens = 0
        cost = 0.0

        expected_key = _OUTPUT_KEY_BY_AGENT.get(agent_name)
        if expected_key and expected_key not in _seen_keys:
            value = state.get(expected_key, "")
            if value:
                _seen_keys.add(expected_key)
                est = estimate_cost("", str(value), model_name)
                tokens += est["output_tokens"]
                cost += est["cost_usd"]

        tracker.record_agent_completion(agent_name, tokens=tokens, cost=cost)

        _logger.info(
            "agent_completed",
            extra={
                "agent_name": agent_name,
                "tokens_used": tracker.tokens_used,
                "cost_usd": round(tracker.cost_usd, 6),
            },
        )
        return None

    return after_agent_track_cost


def wire_agent_callbacks(agent: Any, tracker: BudgetTracker, model_name: str) -> None:
    """Attach budget callbacks to an agent and recursively to its sub-agents.

    ADK fires agent-level callbacks only on the specific agent they are
    attached to; a callback on a parent `SequentialAgent` does not fire per
    sub-agent. To produce one `per_agent` row per stage we attach the same
    tracker's callbacks to every node in the tree.
    """
    try:
        agent.before_agent_callback = make_before_agent_callback(tracker)
        agent.after_agent_callback = make_after_agent_callback(tracker, model_name)
    except (AttributeError, ValueError):
        pass

    for sub in getattr(agent, "sub_agents", []) or []:
        wire_agent_callbacks(sub, tracker, model_name)


def make_before_model_callback(
    tracker: BudgetTracker,
) -> Any:
    """Create an ADK before_model_callback for LLM request logging."""

    async def before_model_log(callback_context: Any, llm_request: Any) -> Any:
        _logger.debug(
            "model_request",
            extra={"tokens_remaining": tracker.tokens_remaining},
        )
        return None

    return before_model_log


def make_after_model_callback(
    tracker: BudgetTracker,
) -> Any:
    """Create an ADK after_model_callback for LLM response token tracking."""

    async def after_model_log(callback_context: Any, llm_response: Any) -> Any:
        _logger.debug(
            "model_response",
            extra={"tokens_used": tracker.tokens_used},
        )
        return None

    return after_model_log
