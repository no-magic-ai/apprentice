"""The effective control policy: the retained configuration limits as one value."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from apprentice.core.config import HOUR, MINUTE, delay
from apprentice.metering.pricing import usd_to_nanodollars

if TYPE_CHECKING:
    from apprentice.core.config import ApprenticeConfig

IMPLEMENTATION_ROLE = "implementation"
TOOL_ROLES = ("instrumentation", "visualization", "assessment")


@dataclass(frozen=True)
class ControlPolicy:
    """The eighteen retained limits, in exact units (USD as whole nanodollars)."""

    monthly_token_ceiling: int
    monthly_cost_ceiling_nanodollars: int
    max_tokens_per_cycle: int
    max_cost_per_cycle_nanodollars: int
    max_tokens_per_stage: int
    max_tokens_per_agent_call: int
    implementation_budget_pct: Decimal
    tool_agent_budget_pct: Decimal
    max_implementation_retries: int
    max_prs_per_day: int
    max_prs_per_week: int
    max_concurrent_items: int
    cooldown_hours: Decimal
    max_files_per_pr: int
    max_lines_per_pr: int
    failure_threshold: int
    half_open_probe_after_minutes: Decimal
    max_open_cycles_before_manual_reset: int

    @classmethod
    def from_config(cls, config: ApprenticeConfig) -> ControlPolicy:
        budget = config.budget
        limits = config.rate_limits
        circuit = config.circuit_breaker
        return cls(
            monthly_token_ceiling=budget.global_budget.monthly_token_ceiling,
            monthly_cost_ceiling_nanodollars=usd_to_nanodollars(
                budget.global_budget.monthly_cost_ceiling_usd
            ),
            max_tokens_per_cycle=budget.cycle.max_tokens_per_cycle,
            max_cost_per_cycle_nanodollars=usd_to_nanodollars(budget.cycle.max_cost_per_cycle_usd),
            max_tokens_per_stage=budget.stage.max_tokens_per_stage,
            max_tokens_per_agent_call=budget.agent.max_tokens_per_agent_call,
            implementation_budget_pct=budget.agent.implementation_budget_pct,
            tool_agent_budget_pct=budget.agent.tool_agent_budget_pct,
            max_implementation_retries=config.agents.max_implementation_retries,
            max_prs_per_day=limits.max_prs_per_day,
            max_prs_per_week=limits.max_prs_per_week,
            max_concurrent_items=limits.max_concurrent_items,
            cooldown_hours=limits.cooldown_hours,
            max_files_per_pr=limits.max_files_per_pr,
            max_lines_per_pr=limits.max_lines_per_pr,
            failure_threshold=circuit.failure_threshold,
            half_open_probe_after_minutes=circuit.half_open_probe_after_minutes,
            max_open_cycles_before_manual_reset=circuit.max_open_cycles_before_manual_reset,
        )

    def to_json(self) -> str:
        return json.dumps(
            {key: str(value) for key, value in asdict(self).items()},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> ControlPolicy:
        """Read a stored policy; it must satisfy the same bounds as a loaded configuration.

        Raises:
            ValueError: On a non-finite or negative number or a delay whose
                deadline cannot be represented (see `config.delay`).
            KeyError, TypeError, ArithmeticError: On a missing or malformed field.
        """
        raw = json.loads(text)
        values: dict[str, int | Decimal] = {}
        for key, field_type in cls.__annotations__.items():
            value = Decimal(raw[key]) if field_type == "Decimal" else int(raw[key])
            if (isinstance(value, Decimal) and not value.is_finite()) or value < 0:
                raise ValueError(
                    f"stored policy {key} {raw[key]!r} is not a finite nonnegative number"
                )
            values[key] = value
        policy = cls(**values)  # type: ignore[arg-type]
        delay(policy.cooldown_hours, HOUR)
        delay(policy.half_open_probe_after_minutes, MINUTE)
        return policy

    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def role_share(self, role: str) -> Decimal | None:
        """Return the cycle percentage allocated to `role`, or None if it has none."""
        if role == IMPLEMENTATION_ROLE:
            return self.implementation_budget_pct
        if role in TOOL_ROLES:
            return self.tool_agent_budget_pct
        return None

    def role_tokens(self, role: str) -> int | None:
        share = self.role_share(role)
        if share is None:
            return None
        return int((self.max_tokens_per_cycle * share / 100).to_integral_value(ROUND_FLOOR))

    def role_nanodollars(self, role: str) -> int | None:
        share = self.role_share(role)
        if share is None:
            return None
        allowance = self.max_cost_per_cycle_nanodollars * share / 100
        return int(allowance.to_integral_value(ROUND_FLOOR))
