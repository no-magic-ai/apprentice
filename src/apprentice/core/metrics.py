"""Metrics aggregation — run lifecycle and usage by accounting category.

Usage comes from the installation's control ledger, which records every
controlled cycle (build, retry, standalone suggest and library work), whether
or not the cycle left a run record. Run records contribute only their
lifecycle (completed / failed / in progress) and, for records written before
the ledger, their per-agent *estimates*. A record's own `accounting` snapshot
is a derived copy of ledger rows and is never counted again.

Usage is never summed into one "cost". The five categories are:

- `historical_estimate`: old output-length estimates from pre-ledger
  records (not metered usage);
- `qualified_price_quote`: settled usage on a paid route at pinned rates
  plus counting-fee bounds — a quote, not an invoice;
- `nonhosted_reference_capacity`: settled non-hosted usage charged at a
  reference model's rates as modelled capacity, not spend;
- `known_zero_hosted`: settled non-hosted usage with known hosted cost zero;
- `unknown_held`: dispatched reservations whose outcome is unknown, at their
  full bound.

Reservations of calls still in flight are reported separately as
`active_reservations`; released reservations (never sent) are not usage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.controls.authority import LedgerUsage
    from apprentice.core.session_store import RunRecord

HISTORICAL = "historical_estimate"
QUOTE = "qualified_price_quote"
REFERENCE = "nonhosted_reference_capacity"
ZERO_HOSTED = "known_zero_hosted"
UNKNOWN = "unknown_held"
CATEGORIES = (HISTORICAL, QUOTE, REFERENCE, ZERO_HOSTED, UNKNOWN)
_SETTLED_CATEGORY = {
    "qualified-price-quote": QUOTE,
    "sdk-reference-capacity": REFERENCE,
    "zero-hosted": ZERO_HOSTED,
}
_ACTIVE_STATES = frozenset({"reserved", "dispatched"})


@dataclass
class UsageCategory:
    """Entries, tokens and amount of one accounting category.

    `nanodollars` holds ledger quotes and holds; `estimated_usd` only the
    historical estimates of records written before the ledger.
    """

    entries: int = 0
    tokens: int = 0
    nanodollars: int = 0
    estimated_usd: Decimal = Decimal(0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "tokens": self.tokens,
            "nanodollars": self.nanodollars,
            "estimated_usd": str(self.estimated_usd),
        }


@dataclass
class RoleMetrics:
    """Settled and unknown generation usage of one role across cycles."""

    role: str
    calls: int = 0
    settled_tokens: int = 0
    unknown_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "calls": self.calls,
            "settled_tokens": self.settled_tokens,
            "unknown_tokens": self.unknown_tokens,
        }


@dataclass
class PipelineReport:
    """Aggregated report: run lifecycle plus ledger usage of the reported cycles.

    `usage_scope` says which ledger cycles the usage covers.
    """

    usage_scope: str
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    in_progress_runs: int = 0
    total_duration_seconds: float = 0.0
    usage: dict[str, UsageCategory] = field(
        default_factory=lambda: {name: UsageCategory() for name in CATEGORIES}
    )
    active_reservations: UsageCategory = field(default_factory=UsageCategory)
    cycles_by_kind: dict[str, int] = field(default_factory=dict)
    per_role: dict[str, RoleMetrics] = field(default_factory=dict)
    per_tier: dict[int, dict[str, int]] = field(default_factory=dict)
    algorithms: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """Completed runs among finished (completed or failed) runs."""
        finished = self.successful_runs + self.failed_runs
        return self.successful_runs / finished if finished > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "in_progress_runs": self.in_progress_runs,
            "success_rate": round(self.success_rate, 4),
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "usage_scope": self.usage_scope,
            "usage": {name: category.to_dict() for name, category in self.usage.items()},
            "active_reservations": self.active_reservations.to_dict(),
            "usage_note": (
                "categories are never added together; quotes and reference capacity are "
                "policy amounts, not invoices, and unknown holds are full reservations"
            ),
            "cycles_by_kind": self.cycles_by_kind,
            "per_role": {name: role.to_dict() for name, role in self.per_role.items()},
            "per_tier": self.per_tier,
            "algorithms": self.algorithms,
        }


def _add_historical(report: PipelineReport, per_agent: dict[str, Any]) -> None:
    category = report.usage[HISTORICAL]
    for agent_data in per_agent.values():
        category.entries += int(agent_data.get("calls", 0))
        category.tokens += int(agent_data.get("tokens_used", 0))
        category.estimated_usd += Decimal(str(agent_data.get("cost_usd", 0.0)))


def _add_ledger(report: PipelineReport, usage: LedgerUsage) -> None:
    for cycle in usage.cycles:
        report.cycles_by_kind[cycle["kind"]] = report.cycles_by_kind.get(cycle["kind"], 0) + 1
    for entry in usage.entries:
        state = entry["state"]
        if state == "released":
            continue
        if state in _ACTIVE_STATES:
            target = report.active_reservations
            tokens, nanos = entry["bound_tokens"], entry["bound_nanodollars"]
            name = None
        elif state == "settled":
            name = _SETTLED_CATEGORY[str(entry["basis"])]
            target = report.usage[name]
            tokens, nanos = entry["charged_tokens"], entry["charged_nanodollars"]
        else:
            name = UNKNOWN
            target = report.usage[UNKNOWN]
            tokens, nanos = entry["bound_tokens"], entry["bound_nanodollars"]
        target.entries += 1
        target.tokens += int(tokens)
        target.nanodollars += int(nanos)
        if name is not None and entry["operation"] == "generate":
            role = report.per_role.setdefault(entry["role"], RoleMetrics(role=entry["role"]))
            role.calls += 1
            if name == UNKNOWN:
                role.unknown_tokens += int(tokens)
            else:
                role.settled_tokens += int(tokens)


def aggregate_runs(
    records: list[RunRecord], usage: LedgerUsage, usage_scope: str
) -> PipelineReport:
    """Aggregate run lifecycle from `records` and usage from the ledger's `usage`.

    Args:
        records: Run records to report the lifecycle of; only records written
            before the ledger contribute (historical) usage estimates.
        usage: Admitted cycles and ledger entries (`Authority.usage`) of the
            reported scope — every source of metered usage; `cycles_by_kind`
            counts every admitted cycle, including ones without an entry.
        usage_scope: Which cycles `usage` covers, reported with the usage.

    Returns:
        PipelineReport with lifecycle, per-tier, per-role and per-category usage.
    """
    report = PipelineReport(usage_scope=usage_scope)
    _add_ledger(report, usage)

    for record in records:
        report.total_runs += 1
        report.total_duration_seconds += record.elapsed_seconds
        tier_entry = report.per_tier.setdefault(
            record.tier, {"total": 0, "success": 0, "fail": 0, "in_progress": 0}
        )
        tier_entry["total"] += 1
        if record.status == "completed":
            report.successful_runs += 1
            tier_entry["success"] += 1
        elif record.status == "in_progress":
            report.in_progress_runs += 1
            tier_entry["in_progress"] += 1
        else:
            report.failed_runs += 1
            tier_entry["fail"] += 1

        budget = record.budget_summary
        if budget and "accounting" not in budget:
            _add_historical(report, budget.get("per_agent", {}))

        report.algorithms.append(
            {
                "run_id": record.run_id,
                "algorithm": record.algorithm_name,
                "tier": record.tier,
                "status": record.status,
                "elapsed_seconds": record.elapsed_seconds,
                "error": record.error,
            }
        )

    return report
