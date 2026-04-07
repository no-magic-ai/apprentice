"""CLI entry point for apprentice."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

    build_parser = subparsers.add_parser("build", help="Run pipeline through review (no packaging)")
    build_parser.add_argument("algorithm", help="Algorithm name to build")
    build_parser.add_argument("--tier", type=int, default=2, help="Algorithm tier (default: 2)")
    build_parser.add_argument(
        "--description", type=str, default="", help="Optional algorithm description"
    )
    build_parser.add_argument(
        "--backend", type=str, default=None, help="Override provider backend (e.g. ollama)"
    )
    build_parser.add_argument(
        "--model", type=str, default=None, help="Override model (e.g. ollama_chat/llama3.3)"
    )

    submit_parser = subparsers.add_parser("submit", help="Package last build into PRs")
    submit_parser.add_argument("algorithm", help="Algorithm name to package")
    submit_parser.add_argument("--tier", type=int, default=2, help="Algorithm tier (default: 2)")
    submit_parser.add_argument("--backend", type=str, default=None, help="Override backend")
    submit_parser.add_argument("--model", type=str, default=None, help="Override model")

    suggest_parser = subparsers.add_parser("suggest", help="Discover candidate algorithms")
    suggest_parser.add_argument("--tier", type=int, default=2, help="Target tier (default: 2)")
    suggest_parser.add_argument("--limit", type=int, default=5, help="Max candidates (default: 5)")
    suggest_parser.add_argument("--backend", type=str, default=None, help="Override backend")
    suggest_parser.add_argument("--model", type=str, default=None, help="Override model")

    retry_parser = subparsers.add_parser("retry", help="Retry a failed pipeline run")
    retry_parser.add_argument("run_id", help="Run ID to retry (from 'apprentice history')")
    retry_parser.add_argument("--backend", type=str, default=None, help="Override backend")
    retry_parser.add_argument("--model", type=str, default=None, help="Override model")

    history_parser = subparsers.add_parser("history", help="List past pipeline runs")
    history_parser.add_argument("--status", type=str, default=None, help="Filter by status")
    history_parser.add_argument("--limit", type=int, default=20, help="Max entries (default: 20)")

    subparsers.add_parser("metrics", help="Show aggregated pipeline metrics")

    subparsers.add_parser("preview", help="Inspect last build artifacts")
    subparsers.add_parser("status", help="Show budget usage and queue state")
    subparsers.add_parser("config", help="Display current configuration")

    dev_parser = subparsers.add_parser("dev", help="Launch ADK dev UI for interactive debugging")
    dev_parser.add_argument("--port", type=int, default=8080, help="Dev UI port (default: 8080)")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    cfg = _load_cfg(args.config)

    if args.command == "build":
        return _cmd_build(cfg, args)
    if args.command == "submit":
        return _cmd_submit(cfg, args)
    if args.command == "suggest":
        return _cmd_suggest(cfg, args)
    if args.command == "retry":
        return _cmd_retry(cfg, args)
    if args.command == "history":
        return _cmd_history(args)
    if args.command == "metrics":
        return _cmd_metrics()
    if args.command == "preview":
        return _cmd_preview()
    if args.command == "status":
        return _cmd_status(cfg)
    if args.command == "config":
        return _cmd_config(cfg)
    if args.command == "dev":
        return _cmd_dev(cfg, args)

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


def _resolve_model(cfg: ApprenticeConfig, args: Any) -> Any:
    """Resolve the LiteLlm model from config with optional CLI overrides."""
    from apprentice.providers.factory import create_model, create_model_from_override

    backend_override = getattr(args, "backend", None)
    model_override = getattr(args, "model", None)

    if model_override:
        backend = backend_override or cfg.provider.backend
        return create_model_from_override(
            model_string=model_override,
            backend=backend,
            local_api_base=cfg.provider.local_api_base,
        )
    if backend_override:
        from apprentice.core.config import ProviderConfig

        override_cfg = ProviderConfig(
            backend=backend_override,
            model=cfg.provider.model,
            fallback_model=cfg.provider.fallback_model,
            local_api_base=cfg.provider.local_api_base,
        )
        return create_model(override_cfg)

    return create_model(cfg.provider)


def _cmd_build(cfg: ApprenticeConfig, args: Any) -> int:
    from apprentice.core.observability import get_logger
    from apprentice.core.orchestrator import build_pipeline, get_budget_tracker_from_pipeline
    from apprentice.core.session_store import SessionStore

    logger = get_logger(__name__)
    logger.info("build started: %s (tier %d)", args.algorithm, args.tier)

    store = SessionStore()
    record = store.create_run(args.algorithm, args.tier)

    model = _resolve_model(cfg, args)
    pipeline = build_pipeline(model, cfg, include_packaging=False)

    start = time.monotonic()
    try:
        session_state = asyncio.run(
            _run_pipeline(pipeline, args.algorithm, args.tier, args.description)
        )
        elapsed = time.monotonic() - start

        tracker = get_budget_tracker_from_pipeline(pipeline)
        budget_summary = tracker.to_dict() if tracker else {}

        has_output = bool(session_state.get("generated_code"))
        if has_output:
            store.complete_run(record, session_state, budget_summary, elapsed)
        else:
            store.fail_run(record, session_state, budget_summary, elapsed, "no output generated")

    except Exception as exc:
        elapsed = time.monotonic() - start
        store.fail_run(record, {}, {}, elapsed, str(exc))
        logger.error("build failed: %s", exc)
        _print_json({"error": str(exc), "run_id": record.run_id})
        return 1

    _print_build_result(args.algorithm, args.tier, session_state, elapsed, record.run_id)
    return 0


def _cmd_submit(cfg: ApprenticeConfig, args: Any) -> int:
    from apprentice.core.observability import get_logger
    from apprentice.core.orchestrator import build_pipeline

    logger = get_logger(__name__)
    logger.info("submit started: %s (tier %d)", args.algorithm, args.tier)

    model = _resolve_model(cfg, args)
    pipeline = build_pipeline(model, cfg, include_packaging=True)

    start = time.monotonic()
    session_state = asyncio.run(_run_pipeline(pipeline, args.algorithm, args.tier, ""))
    elapsed = time.monotonic() - start

    _print_build_result(args.algorithm, args.tier, session_state, elapsed)
    return 0


def _cmd_suggest(cfg: ApprenticeConfig, args: Any) -> int:
    from apprentice.core.observability import get_logger
    from apprentice.core.orchestrator import build_discovery_pipeline

    logger = get_logger(__name__)
    logger.info("suggesting algorithms for tier %d (limit %d)", args.tier, args.limit)

    model = _resolve_model(cfg, args)
    discovery = build_discovery_pipeline(model)

    session_state = asyncio.run(
        _run_agent(discovery, f"Suggest {args.limit} algorithms for tier {args.tier}")
    )

    _print_json(
        {
            "tier": args.tier,
            "candidates": session_state.get("discovery_candidates", ""),
        }
    )
    return 0


async def _run_pipeline(
    pipeline: Any,
    algorithm: str,
    tier: int,
    description: str,
) -> dict[str, Any]:
    """Run the ADK pipeline and return the final session state."""
    from google.adk.agents import InvocationContext, RunConfig
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.events.event import Event
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    session_service = InMemorySessionService()  # type: ignore[no-untyped-call]
    artifact_service = InMemoryArtifactService()

    session = await session_service.create_session(
        app_name="apprentice",
        user_id="cli",
        state={
            "algorithm_name": algorithm,
            "algorithm_tier": tier,
            "description": description,
        },
    )

    user_content = types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    f"Build a complete implementation of the {algorithm} algorithm "
                    f"(tier {tier}). Description: {description or 'N/A'}"
                )
            )
        ],
    )

    user_event = Event(
        invocation_id="build_001",
        author="user",
        content=user_content,
    )
    session.events.append(user_event)

    ctx = InvocationContext(
        invocation_id="build_001",
        agent=pipeline,
        session=session,
        session_service=session_service,
        artifact_service=artifact_service,
        run_config=RunConfig(),
    )

    async for _event in pipeline.run_async(ctx):
        pass

    return dict(session.state)


async def _run_agent(agent: Any, prompt: str) -> dict[str, Any]:
    """Run a single ADK agent and return session state."""
    from google.adk.agents import InvocationContext, RunConfig
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.events.event import Event
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    session_service = InMemorySessionService()  # type: ignore[no-untyped-call]
    artifact_service = InMemoryArtifactService()

    session = await session_service.create_session(
        app_name="apprentice",
        user_id="cli",
    )

    user_content = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    user_event = Event(
        invocation_id="run_001",
        author="user",
        content=user_content,
    )
    session.events.append(user_event)

    ctx = InvocationContext(
        invocation_id="run_001",
        agent=agent,
        session=session,
        session_service=session_service,
        artifact_service=artifact_service,
        run_config=RunConfig(),
    )

    async for _event in agent.run_async(ctx):
        pass

    return dict(session.state)


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
            "provider": {
                "backend": cfg.provider.backend,
                "model": cfg.provider.model,
            },
        }
    )
    return 0


def _cmd_config(cfg: ApprenticeConfig) -> int:
    _print_json(asdict(cfg))
    return 0


def _cmd_dev(cfg: ApprenticeConfig, args: Any) -> int:
    import subprocess

    port = args.port
    _print_json({"message": f"Starting ADK dev UI on port {port}", "command": "adk web"})
    try:
        subprocess.run(
            ["adk", "web", "--port", str(port)],
            check=True,
        )
    except FileNotFoundError:
        _print_json({"error": "adk CLI not found. Install google-adk: uv add google-adk"})
        return 1
    except subprocess.CalledProcessError:
        return 1
    return 0


def _cmd_retry(cfg: ApprenticeConfig, args: Any) -> int:
    from apprentice.core.observability import get_logger
    from apprentice.core.orchestrator import build_pipeline, get_budget_tracker_from_pipeline
    from apprentice.core.session_store import SessionStore

    logger = get_logger(__name__)
    store = SessionStore()

    try:
        old_record = store.load(args.run_id)
    except FileNotFoundError:
        _print_json({"error": f"Run not found: {args.run_id}"})
        return 1

    if old_record.status != "failed":
        _print_json({"error": f"Run {args.run_id} is not failed (status: {old_record.status})"})
        return 1

    algorithm = old_record.algorithm_name
    tier = old_record.tier
    logger.info("retrying: %s (tier %d) from run %s", algorithm, tier, args.run_id)

    model = _resolve_model(cfg, args)
    pipeline = build_pipeline(model, cfg, include_packaging=False)

    new_record = store.create_run(algorithm, tier)
    start = time.monotonic()

    try:
        session_state = asyncio.run(_run_pipeline(pipeline, algorithm, tier, ""))
        elapsed = time.monotonic() - start

        tracker = get_budget_tracker_from_pipeline(pipeline)
        budget_summary = tracker.to_dict() if tracker else {}

        has_output = bool(session_state.get("generated_code"))
        if has_output:
            store.complete_run(new_record, session_state, budget_summary, elapsed)
        else:
            store.fail_run(
                new_record, session_state, budget_summary, elapsed, "no output generated"
            )

    except Exception as exc:
        elapsed = time.monotonic() - start
        store.fail_run(new_record, {}, {}, elapsed, str(exc))
        logger.error("retry failed: %s", exc)
        _print_json({"error": str(exc), "run_id": new_record.run_id})
        return 1

    _print_build_result(algorithm, tier, session_state, elapsed, new_record.run_id)
    return 0


def _cmd_history(args: Any) -> int:
    from apprentice.core.session_store import SessionStore

    store = SessionStore()
    records = store.list_runs(status=args.status, limit=args.limit)

    entries = [
        {
            "run_id": r.run_id,
            "algorithm": r.algorithm_name,
            "tier": r.tier,
            "status": r.status,
            "started_at": r.started_at,
            "elapsed_seconds": r.elapsed_seconds,
            "error": r.error[:100] if r.error else "",
        }
        for r in records
    ]
    _print_json({"runs": entries, "total": len(entries)})
    return 0


def _cmd_metrics() -> int:
    from apprentice.core.metrics import aggregate_runs
    from apprentice.core.session_store import SessionStore

    store = SessionStore()
    records = store.list_runs(limit=100)

    if not records:
        _print_json({"error": "No run records found. Run 'apprentice build' first."})
        return 0

    report = aggregate_runs(records)
    _print_json(report.to_dict())
    return 0


def _print_build_result(
    algorithm: str,
    tier: int,
    session_state: dict[str, Any],
    elapsed: float,
    run_id: str = "",
) -> None:
    _print_json(
        {
            "run_id": run_id,
            "algorithm": algorithm,
            "tier": tier,
            "session_state_keys": list(session_state.keys()),
            "generated_code": bool(session_state.get("generated_code")),
            "instrumented_code": bool(session_state.get("instrumented_code")),
            "manim_scene_code": bool(session_state.get("manim_scene_code")),
            "anki_deck_content": bool(session_state.get("anki_deck_content")),
            "review_verdict": session_state.get("review_verdict", ""),
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
