"""Metrics: run lifecycle from records, cycles and usage only from the ledger."""

from __future__ import annotations

import io
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from rich.console import Console

from apprentice.controls.authority import LedgerUsage
from apprentice.core import progress
from apprentice.core.metrics import aggregate_runs
from apprentice.core.session_store import RunRecord

if TYPE_CHECKING:
    import pytest


def _record(
    name: str = "quicksort",
    tier: int = 2,
    status: str = "completed",
    budget: dict[str, Any] | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=f"{name}-test",
        algorithm_name=name,
        tier=tier,
        status=status,
        budget_summary=budget or {},
        started_at="2026-01-01T00:00:00+00:00",
        elapsed_seconds=10.0,
    )


def _entry(
    state: str, basis: str = "zero-hosted", kind: str = "build", **amounts: int
) -> dict[str, Any]:
    return {
        "cycle_id": amounts.pop("cycle", 1),
        "kind": kind,
        "run_id": None,
        "month": "2026-10",
        "stage": "implementation",
        "role": "implementation",
        "operation": "generate",
        "basis": basis,
        "state": state,
        "bound_tokens": amounts.get("bound_tokens", 0),
        "bound_nanodollars": amounts.get("bound_nanodollars", 0),
        "charged_tokens": amounts.get("charged_tokens"),
        "charged_nanodollars": amounts.get("charged_nanodollars"),
    }


def _usage(entries: list[dict[str, Any]], *entryless: tuple[int, str]) -> LedgerUsage:
    """The ledger read of `entries`' cycles plus admitted cycles that wrote no entry."""
    cycles = {e["cycle_id"]: e["kind"] for e in entries} | dict(entryless)
    return LedgerUsage(
        cycles=[
            {"cycle_id": c, "kind": k, "run_id": None, "state": "terminal", "outcome": "completed"}
            for c, k in cycles.items()
        ],
        entries=entries,
    )


class TestLifecycle:
    def test_in_progress_runs_are_neither_failed_nor_finished(self) -> None:
        records = [
            _record("a", status="completed"),
            _record("b", status="failed"),
            _record("c", status="in_progress"),
        ]

        report = aggregate_runs(records, _usage([]), "test")

        assert (report.successful_runs, report.failed_runs, report.in_progress_runs) == (1, 1, 1)
        assert report.success_rate == 0.5
        assert report.per_tier[2] == {"total": 3, "success": 1, "fail": 1, "in_progress": 1}


class TestLedgerUsage:
    def test_record_less_cycles_and_unsealed_runs_are_counted_from_the_ledger(self) -> None:
        entries = [
            _entry("settled", kind="suggest", cycle=1, charged_tokens=40, charged_nanodollars=0),
            _entry("unknown", kind="build", cycle=2, bound_tokens=5974, bound_nanodollars=0),
            _entry(
                "settled",
                basis="qualified-price-quote",
                kind="library",
                cycle=3,
                charged_tokens=100,
                charged_nanodollars=5_010_000,
            ),
        ]

        report = aggregate_runs([_record(status="in_progress")], _usage(entries), "test")

        assert report.usage["known_zero_hosted"].tokens == 40
        assert report.usage["unknown_held"].tokens == 5974
        assert report.usage["qualified_price_quote"].nanodollars == 5_010_000
        assert report.cycles_by_kind == {"suggest": 1, "build": 1, "library": 1}

    def test_admitted_cycles_without_entries_are_counted_and_add_no_usage(self) -> None:
        entries = [
            _entry("settled", kind="suggest", cycle=1, charged_tokens=40, charged_nanodollars=0)
        ]
        with_entries_only = aggregate_runs([], _usage(entries), "test")

        report = aggregate_runs([], _usage(entries, (2, "suggest"), (3, "build")), "test")

        assert report.cycles_by_kind == {"suggest": 2, "build": 1}
        assert report.to_dict()["usage"] == with_entries_only.to_dict()["usage"]
        assert report.active_reservations.entries == 0

    def test_released_reservations_are_not_usage_and_in_flight_holds_stay_separate(self) -> None:
        entries = [
            _entry("released", cycle=1, bound_tokens=900, bound_nanodollars=0),
            _entry("dispatched", cycle=2, bound_tokens=700, bound_nanodollars=0),
        ]

        report = aggregate_runs([], _usage(entries), "test")

        assert all(category.entries == 0 for category in report.usage.values())
        assert (report.active_reservations.entries, report.active_reservations.tokens) == (1, 700)

    def test_record_snapshots_are_not_counted_again_and_estimates_stay_historical(self) -> None:
        # A sealed record's accounting snapshot (with any estimates beside it)
        # is a derived copy of ledger rows: only the ledger counts its usage.
        snapshot = {
            "accounting": {
                "entries": [_entry("settled", charged_tokens=99, charged_nanodollars=0)]
            },
            "per_agent": {"drafter": {"tokens_used": 5000, "cost_usd": 9.0, "calls": 9}},
        }
        legacy = {"per_agent": {"drafter": {"tokens_used": 1000, "cost_usd": 0.5, "calls": 2}}}
        records = [_record("new", budget=snapshot), _record("old", budget=legacy)]
        entries = [_entry("settled", charged_tokens=99, charged_nanodollars=0)]

        usage = aggregate_runs(records, _usage(entries), "test").usage

        assert usage["known_zero_hosted"].tokens == 99
        assert (
            usage["historical_estimate"].tokens,
            usage["historical_estimate"].estimated_usd,
        ) == (
            1000,
            Decimal("0.5"),
        )


def _flat(text: str) -> str:
    """Rendered text with every whitespace (including deliberate line wraps) removed."""
    return "".join(text.split())


class TestRenderedReport:
    def test_every_category_amount_and_unit_survive_an_80_column_console_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = io.StringIO()
        monkeypatch.setattr(progress, "console", Console(file=out, width=80, force_terminal=False))
        # A legacy record's binary-float estimate and ledger amounts far above
        # any real use: nothing may be rounded, cut or elided.
        legacy = _record(
            "old",
            budget={
                "per_agent": {"drafter": {"tokens_used": 1000, "cost_usd": 0.1 + 0.2, "calls": 2}}
            },
        )
        entries = [
            _entry(
                "settled",
                basis="qualified-price-quote",
                cycle=1,
                charged_tokens=987_654_321_987,
                charged_nanodollars=123_456_789_012_345_678_901,
            ),
            _entry(
                "settled",
                basis="sdk-reference-capacity",
                cycle=2,
                charged_tokens=1863,
                charged_nanodollars=4_770_000,
            ),
            _entry("settled", cycle=3, charged_tokens=40, charged_nanodollars=0),
            _entry("unknown", cycle=4, bound_tokens=5974, bound_nanodollars=66_092_500),
            _entry("dispatched", cycle=5, bound_tokens=700, bound_nanodollars=1_000),
        ]
        running = _record("unsealed", status="in_progress")
        report = aggregate_runs([legacy, running], _usage(entries), "test")

        progress.IntegrationProgress(0, "", "").print_summary(report)

        rendered = out.getvalue()
        flat = _flat(rendered)
        assert "…" not in rendered
        assert max(len(line) for line in rendered.splitlines()) <= 80
        categories = [*report.usage.items(), ("active_reservations", report.active_reservations)]
        for name, category in categories:
            amount = (
                f"{category.estimated_usd}USDest."
                if category.estimated_usd
                else f"{Decimal(category.nanodollars).scaleb(-9).normalize():f}USD"
            )
            expected = f"{name}:{category.entries}entries,{category.tokens:,}tokens,{amount}"
            assert expected in flat, name
        assert "0.30000000000000004USDest." in flat
        assert "123456789012.345678901USD" in flat
        assert "987,654,321,987tokens" in flat
        assert "unsealed" in flat and "inprogress" in flat
