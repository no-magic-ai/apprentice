#!/usr/bin/env python3
"""Integration test harness — runs the full ADK pipeline for multiple algorithms.

Usage:
    uv run python scripts/integration_test.py
    uv run python scripts/integration_test.py --tier 2 --limit 3
    uv run python scripts/integration_test.py --backend local --model openai/<profiled-model>
    uv run python scripts/integration_test.py --report-only

Generates algorithms across tiers as controlled, metered library cycles,
measures success rates and produces a JSON report saved to
~/.apprentice/reports/.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from apprentice.controls.errors import AuthorityError, ControlDeniedError
from apprentice.controls.policy import ControlPolicy
from apprentice.core.artifacts import ArtifactError
from apprentice.core.config import load_config
from apprentice.core.cycles import bootstrap_installation, open_authority
from apprentice.core.metrics import PipelineReport, aggregate_runs
from apprentice.core.observability import get_logger, setup_logging
from apprentice.core.progress import IntegrationProgress, suppress_noisy_loggers
from apprentice.core.session_store import RunRecord, SessionStore, StoreUnavailableError
from apprentice.metering.pricing import PriceAuthorityError
from apprentice.metering.profile import ProfileError
from apprentice.providers.factory import RouteError, resolve_route

if TYPE_CHECKING:
    from apprentice.controls.authority import Authority
    from apprentice.providers.factory import ModelRoute

_REPORT_DIR = Path.home() / ".apprentice" / "reports"

_DEFAULT_ALGORITHMS: dict[int, list[str]] = {
    1: ["insertion_sort", "stack", "linear_search"],
    2: ["merge_sort", "binary_search_tree", "hash_table"],
    3: ["red_black_tree", "a_star_search"],
    4: ["bloom_filter", "skip_list"],
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run integration tests for the apprentice pipeline",
    )
    parser.add_argument("--tier", type=int, default=None, help="Test only this tier")
    parser.add_argument("--limit", type=int, default=None, help="Max algorithms per tier")
    parser.add_argument("--backend", type=str, default=None, help="Override provider backend")
    parser.add_argument("--model", type=str, default=None, help="Override model string")
    parser.add_argument("--config", type=Path, default=None, help="Config file path")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Show report from past runs without running new tests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate setup without running LLM calls",
    )
    return parser.parse_args()


def _select_algorithms(
    tier: int | None,
    limit: int | None,
) -> list[tuple[str, int]]:
    """Select (algorithm_name, tier) pairs based on filters."""
    result: list[tuple[str, int]] = []
    for t, algos in sorted(_DEFAULT_ALGORITHMS.items()):
        if tier is not None and t != tier:
            continue
        selected = algos[:limit] if limit else algos
        for name in selected:
            result.append((name, t))
    return result


def _run_single(
    algorithm: str,
    tier: int,
    store: SessionStore,
    authority: Authority,
    route: ModelRoute,
    policy: ControlPolicy,
    logger: Any,
) -> RunRecord:
    """Run one controlled library build cycle for an algorithm and return its run record.

    Raises:
        ControlDeniedError: If a control refuses the cycle; no run is created.
    """
    from apprentice.core.cycles import run_build

    logger.info("starting: %s (tier %d)", algorithm, tier)
    result = run_build(
        store=store,
        authority=authority,
        route=route,
        policy=policy,
        request=(algorithm, tier, ""),
        kind="library",
    )
    if result.outcome == "completed":
        logger.info("completed: %s in %.1fs", algorithm, result.elapsed)
    else:
        logger.error(
            "%s: %s in %.1fs — %s", result.outcome, algorithm, result.elapsed, result.error
        )
    return result.record


def _save_report(report: PipelineReport) -> Path:
    """Save the report as a JSON file."""
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _REPORT_DIR / f"integration-{ts}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    return path


def main() -> int:
    args = _parse_args()

    try:
        cfg = load_config(args.config)
    except (ValueError, TypeError, KeyError, FileNotFoundError) as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 1
    try:
        footprint = bootstrap_installation(cfg, None)
    except StoreUnavailableError as exc:
        print(f"run store unavailable: {exc}", file=sys.stderr)
        return 1
    except (AuthorityError, ArtifactError) as exc:
        print(f"control authority unavailable: {exc}", file=sys.stderr)
        return 1
    setup_logging(
        {"log_level": cfg.observability.log_level, "log_path": cfg.observability.log_path}
    )
    suppress_noisy_loggers()
    logger = get_logger("integration_test")

    try:
        store = SessionStore()
    except StoreUnavailableError as exc:
        print(f"run store unavailable: {exc}", file=sys.stderr)
        return 1

    if args.report_only:
        try:
            past_records = store.list_runs(limit=None)
        except StoreUnavailableError as exc:
            print(f"run store unavailable: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"Cannot report: {exc}", file=sys.stderr)
            return 1
        try:
            authority = open_authority(store, footprint)
            try:
                usage = authority.usage()
            finally:
                authority.close()
        except AuthorityError as exc:
            print(f"control authority unavailable: {exc}", file=sys.stderr)
            return 1
        report = aggregate_runs(past_records, usage, "installation ledger: every controlled cycle")
        progress = IntegrationProgress(0, "", "")
        progress.print_summary(report)
        return 0

    algorithms = _select_algorithms(args.tier, args.limit)
    backend = args.backend or cfg.provider.backend
    model_str = args.model or cfg.provider.model

    if args.dry_run:
        from rich.console import Console

        console = Console(stderr=True)
        console.print(f"[bold]Would test {len(algorithms)} algorithms:[/]")
        for name, tier in algorithms:
            console.print(f"  [dim]•[/] {name} [dim](tier {tier})[/]")
        console.print(f"[dim]Backend:[/] {backend}")
        console.print(f"[dim]Model:[/] {model_str}")
        return 0

    logger.info("starting integration test: %d algorithms", len(algorithms))

    try:
        route = resolve_route(cfg.provider, backend=args.backend, model=args.model)
    except (RouteError, ProfileError, PriceAuthorityError) as exc:
        print(f"Cannot run: {exc}", file=sys.stderr)
        return 1
    try:
        authority = open_authority(store, footprint)
    except AuthorityError as exc:
        print(f"control authority unavailable: {exc}", file=sys.stderr)
        return 1
    policy = ControlPolicy.from_config(cfg)
    ip = IntegrationProgress(len(algorithms), backend, model_str)
    records: list[RunRecord] = []

    try:
        with ip.start():
            for algorithm, tier in algorithms:
                ip.on_algorithm_start(algorithm, tier)
                try:
                    record = _run_single(algorithm, tier, store, authority, route, policy, logger)
                except ControlDeniedError as exc:
                    print(f"Stopped: {exc}", file=sys.stderr)
                    break
                records.append(record)
                ip.on_algorithm_complete(
                    algorithm,
                    record.status == "completed",
                    record.elapsed_seconds,
                )
        usage = authority.usage({record.run_id for record in records})
    except StoreUnavailableError as exc:
        print(f"run store unavailable: {exc}", file=sys.stderr)
        return 1
    except AuthorityError as exc:
        print(f"control authority unavailable: {exc}", file=sys.stderr)
        return 1
    finally:
        authority.close()

    report = aggregate_runs(records, usage, "ledger cycles of the runs in this report")
    report_path = _save_report(report)

    ip.print_summary(report)
    logger.info("report saved to %s", report_path)

    target_rate = 0.95
    if report.success_rate < target_rate:
        from rich.console import Console

        Console(stderr=True).print(
            f"[bold red]Success rate {report.success_rate * 100:.0f}% "
            f"below target {target_rate * 100:.0f}%[/]"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
