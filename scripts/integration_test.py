#!/usr/bin/env python3
"""Integration test harness — runs the full ADK pipeline for multiple algorithms.

Usage:
    uv run python scripts/integration_test.py
    uv run python scripts/integration_test.py --tier 2 --limit 3
    uv run python scripts/integration_test.py --backend ollama --model ollama_chat/llama3.3
    uv run python scripts/integration_test.py --report-only

Generates algorithms across tiers, measures success rates and costs,
and produces a JSON report saved to ~/.apprentice/reports/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from apprentice.core.config import load_config
from apprentice.core.metrics import PipelineReport, aggregate_runs
from apprentice.core.observability import get_logger, setup_logging
from apprentice.core.session_store import RunRecord, SessionStore

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
    cfg: Any,
    backend: str | None,
    model: str | None,
    store: SessionStore,
    logger: Any,
) -> RunRecord:
    """Run the pipeline for a single algorithm and return the run record."""
    from apprentice.core.orchestrator import build_pipeline
    from apprentice.providers.factory import create_model, create_model_from_override

    if model:
        llm_model = create_model_from_override(
            model_string=model,
            backend=backend or cfg.provider.backend,
            local_api_base=cfg.provider.local_api_base,
        )
    elif backend:
        from apprentice.core.config import ProviderConfig

        override_cfg = ProviderConfig(
            backend=backend,
            model=cfg.provider.model,
            fallback_model=cfg.provider.fallback_model,
            local_api_base=cfg.provider.local_api_base,
        )
        llm_model = create_model(override_cfg)
    else:
        llm_model = create_model(cfg.provider)

    pipeline = build_pipeline(llm_model, cfg, include_packaging=False)
    record = store.create_run(algorithm, tier)

    logger.info("starting: %s (tier %d) [%s]", algorithm, tier, record.run_id)
    start = time.monotonic()

    try:
        from apprentice.cli import _run_pipeline

        session_state = asyncio.run(_run_pipeline(pipeline, algorithm, tier, ""))
        elapsed = time.monotonic() - start

        from apprentice.core.orchestrator import get_budget_tracker_from_pipeline

        tracker = get_budget_tracker_from_pipeline(pipeline)
        budget_summary = tracker.to_dict() if tracker else {}

        has_code = bool(session_state.get("generated_code"))
        if has_code:
            record = store.complete_run(record, session_state, budget_summary, elapsed)
            logger.info("completed: %s in %.1fs", algorithm, elapsed)
        else:
            record = store.fail_run(
                record, session_state, budget_summary, elapsed, "no generated_code in session state"
            )
            logger.warning("failed: %s in %.1fs — no output", algorithm, elapsed)

    except Exception as exc:
        elapsed = time.monotonic() - start
        record = store.fail_run(record, {}, {}, elapsed, str(exc))
        logger.error("error: %s in %.1fs — %s", algorithm, elapsed, exc)

    return record


def _generate_report(records: list[RunRecord]) -> PipelineReport:
    """Generate a report from run records."""
    return aggregate_runs(records)


def _save_report(report: PipelineReport) -> Path:
    """Save the report as a JSON file."""
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _REPORT_DIR / f"integration-{ts}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    return path


def _print_report(report: PipelineReport) -> None:
    """Print a summary of the report."""
    print(json.dumps(report.to_dict(), indent=2, default=str))


def main() -> int:
    args = _parse_args()

    cfg = load_config(args.config)
    setup_logging(
        {"log_level": cfg.observability.log_level, "log_path": cfg.observability.log_path}
    )
    logger = get_logger("integration_test")

    store = SessionStore()

    if args.report_only:
        records = store.list_runs(limit=50)
        if not records:
            print("No past runs found.")
            return 0
        report = _generate_report(records)
        _print_report(report)
        return 0

    algorithms = _select_algorithms(args.tier, args.limit)

    if args.dry_run:
        print(f"Would test {len(algorithms)} algorithms:")
        for name, tier in algorithms:
            print(f"  - {name} (tier {tier})")
        print(f"Backend: {args.backend or cfg.provider.backend}")
        print(f"Model: {args.model or cfg.provider.model}")
        return 0

    logger.info("starting integration test: %d algorithms", len(algorithms))
    records: list[RunRecord] = []

    for algorithm, tier in algorithms:
        record = _run_single(algorithm, tier, cfg, args.backend, args.model, store, logger)
        records.append(record)

    report = _generate_report(records)
    report_path = _save_report(report)

    _print_report(report)
    logger.info("report saved to %s", report_path)

    target_rate = 0.95
    if report.success_rate < target_rate:
        logger.warning(
            "success rate %.1f%% below target %.1f%%",
            report.success_rate * 100,
            target_rate * 100,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
