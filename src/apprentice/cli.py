"""CLI entry point for apprentice."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apprentice.core.config import ApprenticeConfig


def main(argv: list[str] | None = None) -> int:
    """Run the apprentice CLI."""
    parser = argparse.ArgumentParser(
        prog="apprentice",
        description="Agentic Algorithm Factory for no-magic",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_get_version()}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to apprentice.toml (default: config/apprentice.toml)",
    )

    subparsers = parser.add_subparsers(dest="command")

    build_parser = subparsers.add_parser("build", help="Run implementation stage for an algorithm")
    build_parser.add_argument("algorithm", help="Algorithm name to build")
    build_parser.add_argument("--tier", type=int, default=2, help="Algorithm tier (default: 2)")
    build_parser.add_argument(
        "--description", type=str, default="", help="Optional algorithm description"
    )

    subparsers.add_parser("status", help="Show budget usage and queue state")
    subparsers.add_parser("config", help="Display current configuration")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    cfg = _load_cfg(args.config)

    if args.command == "build":
        return _cmd_build(cfg, args.algorithm, args.tier, args.description)
    if args.command == "status":
        return _cmd_status(cfg)
    if args.command == "config":
        return _cmd_config(cfg)

    parser.print_help()
    return 1


def _load_cfg(config_path: Path | None) -> ApprenticeConfig:
    from apprentice.core.config import load_config
    from apprentice.core.observability import setup_logging

    cfg = load_config(config_path)
    setup_logging(
        {
            "log_level": cfg.observability.log_level,
            "log_path": cfg.observability.log_path,
        }
    )
    return cfg


def _cmd_build(cfg: ApprenticeConfig, algorithm: str, tier: int, description: str) -> int:
    from apprentice.core.observability import get_logger, log_stage_metrics
    from apprentice.models.work_item import PipelineContext, WorkItem, WorkItemSource
    from apprentice.providers import create_provider
    from apprentice.stages.implementation import ImplementationStage

    logger = get_logger(__name__)
    logger.info("build started: %s (tier %d)", algorithm, tier)

    provider = create_provider(cfg.provider.default, cfg.provider.model)

    work_item = WorkItem(
        id=f"manual-{algorithm}",
        algorithm_name=algorithm,
        tier=tier,
        source=WorkItemSource.MANUAL,
        rationale=description,
        allocated_tokens=cfg.budget.stage.max_tokens_per_stage,
    )

    context = PipelineContext(
        config={"provider": provider, "references": []},
        budget_remaining_tokens=cfg.budget.stage.max_tokens_per_stage,
        budget_remaining_usd=cfg.budget.cycle.max_cost_per_cycle_usd,
    )

    stage = ImplementationStage()

    estimate = stage.estimate_cost(work_item)
    logger.info(
        "cost estimate: %d input + %d output tokens, $%.6f",
        estimate.estimated_input_tokens,
        estimate.estimated_output_tokens,
        estimate.estimated_cost_usd,
    )

    start = time.monotonic()
    result = stage.execute(work_item, context)
    elapsed = time.monotonic() - start

    log_stage_metrics(
        stage_name=stage.name,
        tokens_used=result.tokens_used,
        cost_usd=result.cost_usd,
        duration_seconds=elapsed,
        passed=True,
    )

    _print_json(
        {
            "algorithm": algorithm,
            "tier": tier,
            "artifacts": result.artifacts,
            "tokens_used": result.tokens_used,
            "cost_usd": result.cost_usd,
            "duration_seconds": round(elapsed, 2),
            "diagnostics": result.diagnostics,
        }
    )
    return 0


def _cmd_status(cfg: ApprenticeConfig) -> int:
    _print_json(
        {
            "budget": {
                "monthly_token_ceiling": cfg.budget.global_budget.monthly_token_ceiling,
                "monthly_cost_ceiling_usd": cfg.budget.global_budget.monthly_cost_ceiling_usd,
                "stage_token_cap": cfg.budget.stage.max_tokens_per_stage,
            },
            "rate_limits": {
                "max_prs_per_day": cfg.rate_limits.max_prs_per_day,
                "max_prs_per_week": cfg.rate_limits.max_prs_per_week,
            },
            "circuit_breaker": {
                "failure_threshold": cfg.circuit_breaker.failure_threshold,
            },
        }
    )
    return 0


def _cmd_config(cfg: ApprenticeConfig) -> int:
    _print_json(asdict(cfg))
    return 0


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _get_version() -> str:
    from apprentice import __version__

    return __version__


if __name__ == "__main__":
    sys.exit(main())
