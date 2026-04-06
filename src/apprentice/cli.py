"""CLI entry point for apprentice."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.core.config import ApprenticeConfig
    from apprentice.core.orchestrator import OrchestrationResult


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

    build_parser = subparsers.add_parser("build", help="Run full pipeline for an algorithm")
    build_parser.add_argument("algorithm", help="Algorithm name to build")
    build_parser.add_argument("--tier", type=int, default=2, help="Algorithm tier (default: 2)")
    build_parser.add_argument(
        "--description", type=str, default="", help="Optional algorithm description"
    )

    suggest_parser = subparsers.add_parser("suggest", help="Discover candidate algorithms")
    suggest_parser.add_argument("--tier", type=int, default=2, help="Target tier (default: 2)")
    suggest_parser.add_argument("--limit", type=int, default=5, help="Max candidates (default: 5)")

    subparsers.add_parser("preview", help="Inspect last build artifacts")
    subparsers.add_parser("status", help="Show budget usage and queue state")
    subparsers.add_parser("config", help="Display current configuration")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    cfg = _load_cfg(args.config)

    if args.command == "build":
        return _cmd_build(cfg, args.algorithm, args.tier, args.description)
    if args.command == "suggest":
        return _cmd_suggest(cfg, args.tier, args.limit)
    if args.command == "preview":
        return _cmd_preview()
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


def _create_orchestrator(cfg: ApprenticeConfig) -> tuple[Any, Any]:
    """Create orchestrator with implementation agent wired."""
    from apprentice.agents.implementation import ImplementationAgent
    from apprentice.core.orchestrator import BudgetAllocation, OrchestratorAgent
    from apprentice.providers import create_provider

    provider = create_provider(cfg.provider.default, cfg.provider.model)
    agents: dict[str, Any] = {"implementation": ImplementationAgent()}
    orchestrator = OrchestratorAgent(agents=agents, provider=provider)

    budget = BudgetAllocation(
        total_tokens=cfg.budget.cycle.max_tokens_per_cycle,
        total_usd=cfg.budget.cycle.max_cost_per_cycle_usd,
        implementation_pct=cfg.budget.agent.implementation_budget_pct,
        tool_agent_pct=cfg.budget.agent.tool_agent_budget_pct,
        review_pct=cfg.budget.agent.review_budget_pct,
    )
    return orchestrator, budget


def _cmd_build(cfg: ApprenticeConfig, algorithm: str, tier: int, description: str) -> int:
    from apprentice.core.observability import get_logger
    from apprentice.models.work_item import WorkItem, WorkItemSource

    logger = get_logger(__name__)
    logger.info("build started: %s (tier %d)", algorithm, tier)

    orchestrator, budget = _create_orchestrator(cfg)

    work_item = WorkItem(
        id=f"manual-{algorithm}",
        algorithm_name=algorithm,
        tier=tier,
        source=WorkItemSource.MANUAL,
        rationale=description,
        allocated_tokens=budget.total_tokens,
    )

    start = time.monotonic()
    result = orchestrator.orchestrate(work_item, budget)
    elapsed = time.monotonic() - start

    _print_orchestration_result(result, elapsed)
    return 0 if result.success else 1


def _cmd_suggest(cfg: ApprenticeConfig, tier: int, limit: int) -> int:
    from apprentice.core.observability import get_logger
    from apprentice.models.work_item import PipelineContext, WorkItem, WorkItemSource
    from apprentice.providers import create_provider
    from apprentice.stages.discovery import DiscoveryStage

    logger = get_logger(__name__)
    logger.info("suggesting algorithms for tier %d (limit %d)", tier, limit)

    provider = create_provider(cfg.provider.default, cfg.provider.model)

    work_item = WorkItem(
        id=f"suggest-tier-{tier}",
        algorithm_name=f"discovery-tier-{tier}",
        tier=tier,
        source=WorkItemSource.DISCOVERY,
        rationale=f"Suggest up to {limit} algorithms for tier {tier}",
    )

    context = PipelineContext(
        config={"provider": provider, "limit": limit},
        budget_remaining_tokens=cfg.budget.stage.max_tokens_per_stage,
        budget_remaining_usd=cfg.budget.cycle.max_cost_per_cycle_usd,
    )

    stage = DiscoveryStage()
    result = stage.execute(work_item, context)

    _print_json(
        {
            "tier": tier,
            "candidates_file": result.artifacts.get("discovery", ""),
            "tokens_used": result.tokens_used,
            "cost_usd": result.cost_usd,
            "diagnostics": result.diagnostics,
        }
    )
    return 0


def _cmd_preview() -> int:
    import tempfile

    artifacts_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    if not artifacts_dir.exists():
        _print_json({"error": "No artifacts found. Run 'apprentice build' first."})
        return 1

    files = sorted(artifacts_dir.iterdir())
    artifacts: dict[str, dict[str, Any]] = {}
    for f in files:
        if f.is_file():
            content = f.read_text(encoding="utf-8")
            preview = content[:500] + ("..." if len(content) > 500 else "")
            artifacts[f.name] = {
                "path": str(f),
                "size_bytes": f.stat().st_size,
                "preview": preview,
            }

    _print_json({"artifacts_dir": str(artifacts_dir), "files": artifacts})
    return 0


def _cmd_status(cfg: ApprenticeConfig) -> int:
    _print_json(
        {
            "budget": {
                "monthly_token_ceiling": cfg.budget.global_budget.monthly_token_ceiling,
                "monthly_cost_ceiling_usd": cfg.budget.global_budget.monthly_cost_ceiling_usd,
                "cycle_token_cap": cfg.budget.cycle.max_tokens_per_cycle,
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


def _print_orchestration_result(result: OrchestrationResult, elapsed: float) -> None:
    bundle = result.artifacts
    agent_summaries = [
        {
            "agent": ar.agent_name,
            "success": ar.success,
            "tokens": ar.tokens_used,
            "cost_usd": ar.cost_usd,
            "attempts": ar.attempt_number,
        }
        for ar in result.agent_results
    ]
    _print_json(
        {
            "success": result.success,
            "work_item": {
                "id": result.work_item.id,
                "algorithm": result.work_item.algorithm_name,
                "status": result.work_item.status.value,
            },
            "artifacts": {
                "implementation": bundle.implementation_path,
                "instrumented": bundle.instrumented_path,
                "manim_scene": bundle.manim_scene_path,
                "anki_deck": bundle.anki_deck_path,
            },
            "agents": agent_summaries,
            "total_tokens": result.total_tokens,
            "total_cost_usd": result.total_cost_usd,
            "duration_seconds": round(elapsed, 2),
        }
    )


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _get_version() -> str:
    from apprentice import __version__

    return __version__


if __name__ == "__main__":
    sys.exit(main())
