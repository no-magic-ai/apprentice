"""CLI entry point for apprentice."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from apprentice.controls.authority import Authority, Cycle
    from apprentice.controls.errors import ControlDeniedError
    from apprentice.controls.footprint import Footprint
    from apprentice.core.artifacts import BundleSnapshot
    from apprentice.core.config import ApprenticeConfig
    from apprentice.core.cycles import BuildResult
    from apprentice.core.session_store import SessionStore
    from apprentice.providers.factory import ModelRoute


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
    _add_route_overrides(build_parser)

    submit_parser = subparsers.add_parser(
        "submit", help="Open PRs with the exact approved bytes of a run (no regeneration)"
    )
    submit_parser.add_argument("algorithm", help="Algorithm the approved run built")
    submit_parser.add_argument(
        "--tier", type=int, default=None, help="Tier the approved run must have (optional)"
    )
    submit_parser.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="Approved run to submit (from 'apprentice history')",
    )

    approve_parser = subparsers.add_parser(
        "approve",
        help="Record a human-review approval for a build run (required before submit)",
    )
    approve_parser.add_argument("run_id", help="Run ID to approve (from 'apprentice history')")
    approve_parser.add_argument(
        "--approver",
        type=str,
        default=None,
        help="Approver identity (defaults to $GITHUB_USER or $USER)",
    )

    suggest_parser = subparsers.add_parser("suggest", help="Discover candidate algorithms")
    suggest_parser.add_argument("--tier", type=int, default=2, help="Target tier (default: 2)")
    suggest_parser.add_argument("--limit", type=int, default=5, help="Max candidates (default: 5)")
    _add_route_overrides(suggest_parser)

    retry_parser = subparsers.add_parser("retry", help="Retry a failed pipeline run")
    retry_parser.add_argument("run_id", help="Run ID to retry (from 'apprentice history')")
    _add_route_overrides(retry_parser)

    history_parser = subparsers.add_parser("history", help="List past pipeline runs")
    history_parser.add_argument("--status", type=str, default=None, help="Filter by status")
    history_parser.add_argument("--limit", type=int, default=20, help="Max entries (default: 20)")

    subparsers.add_parser(
        "metrics", help="Show run lifecycle and ledger usage by accounting category"
    )

    preview_parser = subparsers.add_parser(
        "preview", help="Inspect the sealed artifact bundle of a completed run"
    )
    preview_parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Run ID to preview (default: most recently started completed run)",
    )
    subparsers.add_parser(
        "status", help="Show configured limits, ledger state and route validity/admission"
    )
    subparsers.add_parser("config", help="Display current configuration")

    controls_parser = subparsers.add_parser("controls", help="Operator actions on durable controls")
    controls_sub = controls_parser.add_subparsers(dest="controls_command", required=True)
    adopt_parser = controls_sub.add_parser(
        "adopt-legacy",
        help=(
            "Declare that no earlier apprentice process is running, so run records written "
            "without the control authority stop blocking admission (holds are kept)"
        ),
    )
    adopt_parser.add_argument("--operator", type=str, required=True, help="Operator identity")
    adopt_parser.add_argument(
        "--declare-no-earlier-process-running",
        action="store_true",
        required=True,
        help="Required: the operator's own declaration of quiescence",
    )
    reset_parser = controls_sub.add_parser(
        "reset-circuit",
        help=(
            "Close the automated-work circuit after investigating its failures (budgets, holds, "
            "PR slots, quarantine and approvals are untouched)"
        ),
    )
    reset_parser.add_argument("--operator", type=str, required=True, help="Operator identity")
    rollback_parser = controls_sub.add_parser(
        "prepare-rollback",
        help=(
            "Suspend continuity before reverting to a version without these controls; nothing "
            "is admitted until a later `controls adopt-legacy`"
        ),
    )
    rollback_parser.add_argument("--operator", type=str, required=True, help="Operator identity")

    dev_parser = subparsers.add_parser("dev", help="Launch ADK dev UI for interactive debugging")
    dev_parser.add_argument("--port", type=int, default=8080, help="Dev UI port (default: 8080)")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    from apprentice.controls.errors import AuthorityError
    from apprentice.core.artifacts import ArtifactError
    from apprentice.core.config import load_config
    from apprentice.core.cycles import (
        OperatorTermination,
        bootstrap_installation,
        clear_operator_stop,
        operator_stop,
        reached_end,
    )
    from apprentice.core.observability import setup_logging
    from apprentice.core.session_store import StoreUnavailableError

    try:
        cfg = load_config(args.config)
    except (ValueError, TypeError, KeyError, FileNotFoundError) as exc:
        _print_json({"error": f"invalid configuration: {exc}"})
        return 1
    # The authority is created from the earlier footprint before logging (or
    # any command) can create state of its own, whatever the command is.
    try:
        footprint = bootstrap_installation(cfg, None)
    except StoreUnavailableError as exc:
        _print_json({"error": f"run store unavailable: {exc}"})
        return 1
    except (AuthorityError, ArtifactError) as exc:
        _print_json({"error": f"control authority unavailable: {exc}"})
        return 1
    setup_logging(
        {"log_level": cfg.observability.log_level, "log_path": cfg.observability.log_path}
    )

    # SIGTERM to this CLI process is an operator cancellation, handled like
    # Ctrl-C (only for the duration of this command), except that it waits
    # while a cycle's admission or end records are being committed. It is
    # reported with the outcome this command's cycle committed (for example
    # `completed` when it arrived while that end was being recorded), and
    # with no outcome when no cycle ended.
    def terminate(signum: int, frame: object) -> None:
        operator_stop()

    clear_operator_stop()
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        return _dispatch(cfg, footprint, args, parser)
    except StoreUnavailableError as exc:
        # Raised where a command opens or reads the store before any admission, or
        # when a build's run cannot be created (its cycle has already ended).
        _print_json({"error": f"run store unavailable: {exc}"})
        return 1
    except OperatorTermination as stop:
        # Notes name what could not be saved on the way out (for example the run record).
        error = "; ".join(["terminated by the operator (SIGTERM)", *getattr(stop, "__notes__", [])])
        _print_json({"error": error, **reached_end()})
        return 128 + signal.SIGTERM
    finally:
        signal.signal(signal.SIGTERM, previous)
        clear_operator_stop()


def _dispatch(
    cfg: ApprenticeConfig, footprint: Footprint, args: Any, parser: argparse.ArgumentParser
) -> int:
    if args.command == "build":
        return _cmd_build(cfg, footprint, args)
    if args.command == "submit":
        return _cmd_submit(cfg, footprint, args)
    if args.command == "approve":
        return _cmd_approve(args)
    if args.command == "suggest":
        return _cmd_suggest(cfg, footprint, args)
    if args.command == "retry":
        return _cmd_retry(cfg, footprint, args)
    if args.command == "history":
        return _cmd_history(args)
    if args.command == "metrics":
        return _cmd_metrics(footprint)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "status":
        return _cmd_status(cfg, footprint)
    if args.command == "controls":
        return _cmd_controls(footprint, args)
    if args.command == "config":
        return _cmd_config(cfg)
    if args.command == "dev":
        return _cmd_dev(cfg, args)

    parser.print_help()
    return 1


def _add_route_overrides(parser: argparse.ArgumentParser) -> None:
    from apprentice.core.config import SUPPORTED_BACKENDS

    parser.add_argument(
        "--backend",
        type=str,
        default=None,
        help=f"Override provider backend ({', '.join(SUPPORTED_BACKENDS)})",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override model; the accounting profile must qualify it",
    )


def _resolve_route(cfg: ApprenticeConfig, args: Any) -> ModelRoute | None:
    """Resolve the qualified route, or print why it cannot be used (nothing is admitted)."""
    from apprentice.metering.pricing import PriceAuthorityError
    from apprentice.metering.profile import ProfileError
    from apprentice.providers.factory import RouteError, resolve_route

    try:
        return resolve_route(
            cfg.provider,
            backend=getattr(args, "backend", None),
            model=getattr(args, "model", None),
        )
    except (RouteError, ProfileError, PriceAuthorityError) as exc:
        _print_json({"error": str(exc), "denied_before": "any cycle admission or request"})
        return None


def _open_authority(store: SessionStore, footprint: Footprint) -> Authority | None:
    from apprentice.controls.errors import AuthorityError
    from apprentice.core.cycles import open_authority

    try:
        return open_authority(store, footprint)
    except AuthorityError as exc:
        _print_json({"error": f"control authority unavailable: {exc}"})
        return None


def _print_denial(exc: ControlDeniedError, **extra: object) -> None:
    _print_json({"error": str(exc), "control": exc.control, "outcome": "denied", **extra})


def _cmd_build(cfg: ApprenticeConfig, footprint: Footprint, args: Any) -> int:
    from apprentice.core.progress import PipelineProgress, suppress_noisy_loggers

    suppress_noisy_loggers()
    progress = PipelineProgress(args.algorithm, args.tier)
    result = _controlled_build(
        cfg, footprint, args, (args.algorithm, args.tier, args.description), "build", progress
    )
    if result is None:
        return 1
    progress.finish(result.outcome == "completed", result.elapsed)
    if result.outcome != "completed":
        _print_build_failure(result)
        return 1
    progress.print_result(result.session_state, result.record.run_id)
    return 0


def _controlled_build(
    cfg: ApprenticeConfig,
    footprint: Footprint,
    args: Any,
    request: tuple[str, int, str],
    kind: str,
    progress: Any,
) -> BuildResult | None:
    """Run one controlled build cycle, or print why none was admitted."""
    from apprentice.controls.errors import AuthorityError, ControlDeniedError
    from apprentice.controls.policy import ControlPolicy
    from apprentice.core.artifacts import ArtifactError
    from apprentice.core.cycles import run_build
    from apprentice.core.session_store import SessionStore

    try:
        SessionStore.new_run_id(request[0], request[1])
    except (ArtifactError, ValueError) as exc:
        _print_json({"error": str(exc)})
        return None
    route = _resolve_route(cfg, args)
    if route is None:
        return None
    store = SessionStore()
    authority = _open_authority(store, footprint)
    if authority is None:
        return None
    try:
        return run_build(
            store=store,
            authority=authority,
            route=route,
            policy=ControlPolicy.from_config(cfg),
            request=request,
            kind=kind,
            progress=progress,
        )
    except ControlDeniedError as exc:
        _print_denial(exc)
        return None
    except AuthorityError as exc:
        _print_json({"error": f"control authority unavailable: {exc}"})
        return None
    finally:
        authority.close()


def _print_build_failure(result: BuildResult) -> None:
    output: dict[str, Any] = {
        "error": result.error,
        "run_id": result.record.run_id,
        "outcome": result.outcome,
        "accounting": result.accounting,
    }
    if result.gate is not None:
        output["gate"] = result.gate
    if result.controls:
        output["control"] = result.controls[0]
    _print_json(output)


def _cmd_submit(cfg: ApprenticeConfig, footprint: Footprint, args: Any) -> int:
    """Promote the exact approved bytes of a run as one controlled publication cycle.

    No model or generation runs. Before any claim is saved, the approval and
    the run's attempt history are checked, then one `submit` cycle is
    admitted together with one PR slot per intended repository (item lease,
    cooldown, circuit and rolling PR windows in one transaction). Every
    commit is prepared and measured before the final admission persists the
    remote-write intent immediately before the first push. An attempt that
    ended before that intent wrote nothing; the same approved manifest may
    be submitted again. Its outcome is recorded only if the stored attempt is
    still the one this process reserved.
    """
    from apprentice.agents.packaging import PackagingError, intended_repositories
    from apprentice.controls.errors import AuthorityError, ControlDeniedError
    from apprentice.controls.policy import ControlPolicy
    from apprentice.core.cycles import (
        classify,
        describe_error,
        end_cycle,
        open_authority,
        recording_end,
    )
    from apprentice.core.session_store import SessionStore

    store = SessionStore()
    opened: list[Authority] = []

    def authority_handle() -> Authority:
        # Opened only once a claim may follow: a refused run gains no cycle,
        # claim, PR slot or remote effect. (On a fresh installation `main` has
        # already created the authority itself, before any log, which admits
        # nothing.)
        if not opened:
            opened.append(open_authority(store, footprint))
        return opened[0]

    try:
        checked = _check_submission(store, args, authority_handle)
        if checked is None:
            return 1
        snapshot, _approval, previous = checked
        try:
            repositories = intended_repositories(snapshot)
        except PackagingError as exc:
            _print_json({"error": str(exc), "run_id": snapshot.run_id})
            return 1
        authority = authority_handle()
        # Operator stops wait while the admission, the claim and the ends are
        # committed; the remote work itself stays interruptible (`_publish`).
        with recording_end():
            try:
                cycle = authority.begin_cycle(
                    "submit",
                    ControlPolicy.from_config(cfg),
                    run_id=snapshot.run_id,
                    publication_claim=(snapshot.manifest_sha256, repositories),
                )
            except ControlDeniedError as exc:
                _print_denial(exc, run_id=snapshot.run_id, approval="kept; nothing was claimed")
                return 1
            try:
                return _publish(store, authority, cycle, args, previous)
            except BaseException as exc:
                # Any other end — an uncaught error or interruption — commits
                # the terminal state, which closes an attempt that never
                # recorded a write intent as NO_WRITE and releases its slots.
                end_cycle(cycle, classify(exc), describe_error(exc))
                raise
    except AuthorityError as exc:
        _print_json({"error": f"control authority unavailable: {exc}"})
        return 1
    finally:
        for handle in opened:
            handle.close()


def _check_submission(
    store: SessionStore, args: Any, authority: Callable[[], Authority]
) -> tuple[BundleSnapshot, dict[str, Any], dict[str, Any]] | None:
    """Verify the approval and that the run may start a new attempt; save nothing.

    Returns the snapshot, the approval and the previous attempt (empty when
    none), or None after printing why the run cannot be submitted.
    """
    from apprentice.core.artifacts import ArtifactError
    from apprentice.gates.review import ApprovalError, require_approved_snapshot

    try:
        with store.record_lock(args.run_id):
            record = store.load(args.run_id)
            try:
                snapshot = require_approved_snapshot(
                    store, record, algorithm=args.algorithm, tier=args.tier
                )
            except ApprovalError as exc:
                _print_json(
                    {"error": str(exc), "run_id": record.run_id, "remediation": exc.remediation}
                )
                return None
            if not isinstance(record.submission, dict):
                _print_json(
                    {
                        "error": f"stored submission attempt of run {record.run_id} is not an object",
                        "run_id": record.run_id,
                        "submission": record.submission,
                    }
                )
                return None
            if record.submission and not _proven_unwritten(
                authority, record.run_id, snapshot.manifest_sha256, record.submission
            ):
                # An attempt whose effects may be incomplete or unknown is never
                # retried, resumed or overwritten.
                _print_json(
                    {
                        "error": (
                            f"run {record.run_id} already has a submission attempt that may "
                            "have written remotely; it is not published again"
                        ),
                        "run_id": record.run_id,
                        "submission": record.submission,
                    }
                )
                return None
            return snapshot, dict(record.approval), dict(record.submission)
    except (FileNotFoundError, ValueError) as exc:
        _print_json({"error": str(exc)})
        return None
    except ArtifactError as exc:
        _print_json({"error": str(exc), "run_id": args.run_id})
        return None


def _proven_unwritten(
    authority: Callable[[], Authority],
    run_id: str,
    manifest_sha256: str,
    submission: dict[str, Any],
) -> bool:
    """True only if the ledger proves this run's stored attempt wrote nothing remotely.

    The ledger attempt the record names must have ended `NO_WRITE` without a
    remote-write intent, and it must be an attempt of `run_id` for the
    approved manifest `manifest_sha256` — the record's own claim of either is
    not evidence. Reading it commits owner-loss recovery first, so the
    attempt of a killed process has ended. Attempts written before the
    ledger (no attempt ID) are never proven.
    """
    from apprentice.controls.publication import NO_WRITE

    attempt_id = submission.get("attempt_id")
    if not isinstance(attempt_id, str) or submission.get("remote_write_intent") is not False:
        return False
    stored = authority().publication_attempt(attempt_id)
    return (
        stored is not None
        and stored["state"] == NO_WRITE
        and not stored["remote_write_intent"]
        and stored["run_id"] == run_id
        and stored["manifest_sha256"] == manifest_sha256
        and submission.get("manifest_sha256") == manifest_sha256
    )


def _publish(
    store: SessionStore,
    authority: Authority,
    cycle: Cycle,
    args: Any,
    previous: dict[str, Any],
) -> int:
    """Save the pending claim, then prepare, admit and publish; record the outcome once."""
    from datetime import UTC, datetime

    from apprentice.agents.packaging import PublicationSteps, submit_snapshot
    from apprentice.controls.errors import AuthorityError, ControlDeniedError
    from apprentice.controls.publication import COMPLETE, NO_WRITE, WRITE_FAILED
    from apprentice.core.cycles import end_cycle, interruptible
    from apprentice.core.observability import get_logger

    logger = get_logger(__name__)
    assert cycle.publication_attempt is not None
    attempt_id = cycle.publication_attempt
    reservation = _reserve_submission(store, authority, args, cycle, previous)
    if reservation is None:
        authority.finish_publication(attempt_id, NO_WRITE, "claim not saved")
        end_cycle(cycle, "denied", "the run's claim could not be saved")
        return 1
    snapshot, approval, reserved = reservation

    def admit(prepared: list[Any]) -> None:
        policy = cycle.policy
        for submission in prepared:
            if submission.size.files > policy.max_files_per_pr:
                raise ControlDeniedError(
                    "rate_limits.max_files_per_pr",
                    f"{submission.repository} changes {submission.size.files} files; "
                    f"at most {policy.max_files_per_pr} per PR",
                )
            if submission.size.text_lines > policy.max_lines_per_pr:
                raise ControlDeniedError(
                    "rate_limits.max_lines_per_pr",
                    f"{submission.repository} changes {submission.size.text_lines} text lines; "
                    f"at most {policy.max_lines_per_pr} per PR",
                )
        authority.begin_writes(cycle)

    steps = PublicationSteps(
        admit=admit, before_step=lambda step: authority.require_writing(cycle, step)
    )
    logger.info("submit started: run %s manifest %s", snapshot.run_id, snapshot.manifest_sha256)
    try:
        with interruptible():
            submissions = submit_snapshot(snapshot, approval, Path(reserved["workspace"]), steps)
    except BaseException as exc:
        # The live effects, whatever ended the attempt: a step that was started
        # but not acknowledged is None (may have taken effect).
        known = [item.to_dict() for item in steps.progress]
        errors: list[str] = []
        wrote: bool | None
        try:
            stored = authority.publication_attempt(attempt_id)
            wrote = bool(stored and stored["remote_write_intent"])
        except AuthorityError as fault:
            errors.append(f"control authority unavailable: {fault}")
            # Known effects mean the intent was recorded; without them it may
            # have been, so the attempt is never taken as unwritten.
            wrote = True if known else None
        effects = known if wrote is not False else []
        outcome: dict[str, Any] = {
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "error": str(exc),
            "remote_write_intent": wrote,
            "repositories": effects,
        }
        end_state: str | None
        if wrote is False:
            outcome["status"] = "denied" if isinstance(exc, ControlDeniedError) else "failed"
            end_state = NO_WRITE
        else:
            # A push whose outcome is unknown (None) may have published, so it is partial.
            pushed = any(effect["pushed"] is not False for effect in effects)
            outcome["status"] = "partial" if pushed else "failed"
            outcome["effects_unknown"] = wrote is None or any(
                e["pushed"] is None or e["pr_url"] is None for e in effects
            )
            end_state = WRITE_FAILED if wrote else None
        cycle_outcome = (
            "cancelled"
            if isinstance(exc, KeyboardInterrupt)
            else "denied"
            if isinstance(exc, ControlDeniedError)
            else "failed"
        )
        errors += _end_publication(cycle, end_state, str(exc), cycle_outcome)
        if errors:
            outcome["authority_errors"] = errors
        interruption = None if isinstance(exc, Exception) else exc
        recorded = _finish_submission(store, snapshot.run_id, reserved, outcome, stop=interruption)
        if interruption is not None:
            raise
        if recorded is not None:
            extra = {"control": exc.control} if isinstance(exc, ControlDeniedError) else {}
            _print_json({"error": str(exc), "run_id": snapshot.run_id, **extra, **recorded})
        return 1

    outcome = {
        "status": "complete",
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "remote_write_intent": True,
        "repositories": [submission.to_dict() for submission in submissions],
    }
    errors = _end_publication(cycle, COMPLETE, "", "completed")
    if errors:
        outcome["authority_errors"] = errors
    recorded = _finish_submission(store, snapshot.run_id, reserved, outcome, stop=None)
    if recorded is None:
        return 1
    if errors:
        _print_json({"error": errors[0], "run_id": snapshot.run_id, **recorded})
        return 1
    _print_json({"run_id": snapshot.run_id, **recorded})
    return 0


def _end_publication(cycle: Cycle, state: str | None, detail: str, outcome: str) -> list[str]:
    """Record the attempt's end (`state`; None when unknown) and the cycle's; return failures.

    A failure is reported, never raised, so the caller still records and
    prints every known remote effect. Whatever is not committed stays
    conservative: an unfinished attempt is closed by recovery (written-nothing
    only without a write intent) and a cycle whose end cannot be committed
    keeps its lease until this process ends.
    """
    from apprentice.controls.errors import AuthorityError
    from apprentice.core.cycles import end_cycle

    errors: list[str] = []
    assert cycle.publication_attempt is not None
    if state is not None:
        try:
            cycle.authority.finish_publication(cycle.publication_attempt, state, detail)
        except AuthorityError as exc:
            errors.append(f"control authority unavailable: {exc}")
    try:
        end_cycle(cycle, outcome, detail)
    except AuthorityError as exc:
        errors.append(f"control authority unavailable: {exc}")
    return errors


def _reserve_submission(
    store: SessionStore,
    authority: Authority,
    args: Any,
    cycle: Cycle,
    previous: dict[str, Any],
) -> tuple[BundleSnapshot, dict[str, Any], dict[str, Any]] | None:
    """Under the record lock, re-verify and save the pending claim of `cycle`'s attempt.

    The run's stored attempt must still be exactly `previous` (none, or one
    the ledger proves wrote nothing); that attempt is kept in
    `previous_attempts`. Returns the captured snapshot, the approval read
    under the lock and the reserved attempt, or None after printing why.
    """
    from apprentice.core.artifacts import ArtifactError
    from apprentice.core.cycles import note_for_stop
    from apprentice.core.session_store import StoreUnavailableError
    from apprentice.gates.review import ApprovalError, require_approved_snapshot

    try:
        with store.record_lock(args.run_id):
            record = store.load(args.run_id)
            try:
                snapshot = require_approved_snapshot(
                    store, record, algorithm=args.algorithm, tier=args.tier
                )
            except ApprovalError as exc:
                _print_json(
                    {"error": str(exc), "run_id": record.run_id, "remediation": exc.remediation}
                )
                return None
            if record.submission != previous or (
                previous
                and not _proven_unwritten(
                    lambda: authority, record.run_id, snapshot.manifest_sha256, previous
                )
            ):
                _print_json(
                    {
                        "error": f"run {record.run_id} gained another submission attempt",
                        "run_id": record.run_id,
                        "submission": record.submission,
                    }
                )
                return None
            history = list(previous.pop("previous_attempts", [])) if previous else []
            if previous:
                history.append(previous)
            workspace = store.allocate_work_root()
            reserved: dict[str, Any] = {
                "status": "pending",
                # The ledger's claim instant: never later than the clock of
                # any later ledger decision that reads this record.
                "started_at": cycle.admitted_at.isoformat(),
                "attempt_id": cycle.publication_attempt,
                "remote_write_intent": False,
                "manifest_sha256": snapshot.manifest_sha256,
                "workspace": str(workspace),
                "branch": f"apprentice/{record.run_id}",
                "repositories": [],
            }
            if history:
                reserved["previous_attempts"] = history
            record.submission = dict(reserved)
            try:
                store.save(record)
            except StoreUnavailableError as exc:
                # The claim was not stored (the record keeps its previous attempt); an
                # operator stop deferred until the cycle's end reports it too.
                note_for_stop(
                    f"submission record of run {record.run_id} was not updated: "
                    f"saving the claim (scratch root {workspace}) failed: {exc}"
                )
                raise
            return snapshot, dict(record.approval), reserved
    except (FileNotFoundError, ValueError) as exc:
        _print_json({"error": str(exc)})
        return None
    except ArtifactError as exc:
        _print_json({"error": str(exc), "run_id": args.run_id})
        return None


def _finish_submission(
    store: SessionStore,
    run_id: str,
    reserved: dict[str, Any],
    outcome: dict[str, Any],
    *,
    stop: BaseException | None,
) -> dict[str, Any] | None:
    """Record the outcome of the reserved attempt and return the stored attempt.

    The record is reloaded under the lock and updated only if its attempt is
    still exactly `reserved`; otherwise nothing is saved, and the known
    effects of this process are printed with the discrepancy (returns None).
    `stored` in that output is the attempt read from disk, or None when the
    record could not be read. `stop` is the interruption the caller raises
    again afterwards (None when it raises nothing); it is told the record was
    not updated.
    """
    from apprentice.core.artifacts import ArtifactError
    from apprentice.core.cycles import note_for_stop
    from apprentice.core.session_store import StoreUnavailableError

    stored: object
    try:
        with store.record_lock(run_id):
            latest = store.load(run_id)
            if latest.submission == reserved:
                latest.submission = {**reserved, **outcome}
                try:
                    store.save(latest)
                except StoreUnavailableError as exc:
                    # The atomic replace did not happen: the record still holds `reserved`.
                    stored, discrepancy = reserved, f"saving the outcome failed: {exc}"
                else:
                    return latest.submission
            else:
                stored = latest.submission
                discrepancy = "the stored submission attempt is not the one this process reserved"
    except (OSError, ValueError, ArtifactError) as exc:
        stored, discrepancy = None, str(exc)
    unsaved = f"submission record of run {run_id} was not updated: {discrepancy}"
    # An operator stop deferred until the submit's end is committed reports it too,
    # and so does the stop that interrupted the publication and is raised again.
    note_for_stop(unsaved)
    if stop is not None:
        stop.add_note(unsaved)
    _print_json(
        {
            "error": unsaved,
            "run_id": run_id,
            "reserved": reserved,
            "stored": stored,
            "outcome": outcome,
        }
    )
    return None


def _cmd_approve(args: Any) -> int:
    """Record a human-review approval bound to a completed run's sealed bundle.

    The approval is written under the run's record lock and is refused once
    the run has a submission attempt, so the approval an attempt publishes
    can no longer change.
    """
    import os
    from datetime import UTC, datetime

    from apprentice.core.artifacts import ArtifactError
    from apprentice.core.session_store import SessionStore
    from apprentice.gates.review import (
        ApprovalError,
        approver_problem,
        require_reviewable_snapshot,
    )

    if args.approver is not None:
        approver = args.approver
    else:
        approver = os.environ.get("GITHUB_USER") or os.environ.get("USER") or ""
        if not approver:
            _print_json(
                {
                    "error": (
                        "cannot determine approver — pass --approver or set "
                        "GITHUB_USER/USER environment variable."
                    )
                }
            )
            return 1
    problem = approver_problem(approver)
    if problem:
        _print_json({"error": f"invalid approver: {problem}"})
        return 1

    store = SessionStore()
    try:
        with store.record_lock(args.run_id):
            record = store.load(args.run_id)
            if record.status != "completed":
                _print_json(
                    {
                        "error": (
                            f"Run {args.run_id} is not completed "
                            f"(status: {record.status}); nothing to approve."
                        )
                    }
                )
                return 1
            if not isinstance(record.submission, dict):
                _print_json(
                    {
                        "error": f"stored submission attempt of run {record.run_id} is not an object",
                        "run_id": record.run_id,
                        "submission": record.submission,
                    }
                )
                return 1
            if record.submission:
                _print_json(
                    {
                        "error": (
                            f"run {record.run_id} already has a submission attempt; "
                            "its approval can no longer change"
                        ),
                        "run_id": record.run_id,
                        "submission": record.submission,
                    }
                )
                return 1
            try:
                snapshot = require_reviewable_snapshot(store, record)
            except ApprovalError as exc:
                _print_json(
                    {"error": str(exc), "run_id": args.run_id, "remediation": exc.remediation}
                )
                return 1
            record.approval = {
                "run_id": snapshot.run_id,
                "algorithm": snapshot.algorithm,
                "tier": snapshot.tier,
                "manifest_sha256": snapshot.manifest_sha256,
                "approved_by": approver,
                "approved_at": datetime.now(tz=UTC).isoformat(),
            }
            store.save(record)
    except (FileNotFoundError, ValueError) as exc:
        _print_json({"error": str(exc)})
        return 1
    except ArtifactError as exc:
        _print_json({"error": str(exc), "run_id": args.run_id})
        return 1

    _print_json(
        {
            "approved": True,
            "run_id": snapshot.run_id,
            "algorithm": snapshot.algorithm,
            "tier": snapshot.tier,
            "manifest_sha256": snapshot.manifest_sha256,
            "approved_by": approver,
            "artifacts": snapshot.describe(),
        }
    )
    return 0


def _cmd_suggest(cfg: ApprenticeConfig, footprint: Footprint, args: Any) -> int:
    """Run discovery as one controlled `suggest` cycle (its own cycle, sharing monthly usage)."""
    from apprentice.core.cycles import recording_end
    from apprentice.core.observability import get_logger
    from apprentice.core.session_store import SessionStore

    logger = get_logger(__name__)
    logger.info("suggesting algorithms for tier %d (limit %d)", args.tier, args.limit)

    route = _resolve_route(cfg, args)
    if route is None:
        return 1
    store = SessionStore()
    authority = _open_authority(store, footprint)
    if authority is None:
        return 1
    with recording_end():
        return _suggest_cycle(authority, cfg, route, args)


def _suggest_cycle(
    authority: Authority, cfg: ApprenticeConfig, route: ModelRoute, args: Any
) -> int:
    """Admit, run and end one `suggest` cycle (operator stops deferred outside the model work)."""
    from apprentice.controls.errors import AuthorityError, ControlDeniedError
    from apprentice.controls.policy import ControlPolicy
    from apprentice.core.cycles import (
        classify,
        denied_controls,
        describe_error,
        interruptible,
        run_agent,
    )
    from apprentice.core.orchestrator import build_discovery_pipeline

    try:
        cycle = authority.begin_cycle("suggest", ControlPolicy.from_config(cfg))
    except ControlDeniedError as exc:
        authority.close()
        _print_denial(exc)
        return 1
    except AuthorityError as exc:
        authority.close()
        _print_json({"error": f"control authority unavailable: {exc}"})
        return 1
    try:
        discovery = build_discovery_pipeline(route, cycle)
        with interruptible():
            session_state = asyncio.run(
                run_agent(discovery, f"Suggest {args.limit} algorithms for tier {args.tier}")
            )
    except BaseException as exc:
        outcome = classify(exc)
        output: dict[str, Any] = {"error": describe_error(exc), "outcome": outcome}
        controls = denied_controls(exc)
        if controls:
            output["control"] = controls[0]
        ended = _end_cycle(cycle, outcome, describe_error(exc), output)
        authority.close()
        if outcome == "cancelled":
            if not ended:
                _print_json(output)
            raise
        _print_json(output)
        return 1
    candidates = session_state.get("discovery_candidates")
    if not isinstance(candidates, str) or not candidates.strip():
        # No model output at all is an admitted infrastructure failure, like a
        # build without output; an actual answer — even an empty list "[]" —
        # completes the cycle.
        output = {"error": "no output generated", "outcome": "failed"}
        _end_cycle(cycle, "failed", "no output generated", output)
        authority.close()
        _print_json(output)
        return 1
    output = {"tier": args.tier, "candidates": candidates}
    ended = _end_cycle(cycle, "completed", "", output)
    authority.close()
    _print_json(output)
    return 0 if ended else 1


def _end_cycle(cycle: Cycle, outcome: str, detail: str, output: dict[str, Any]) -> bool:
    """Commit the cycle's terminal outcome and add its ledger rows to `output`.

    If the ledger cannot record it, `output` names the unavailable authority
    instead; the cycle's lease then stays held until this process ends and
    recovery keeps any dispatched bound as unknown. Returns whether it ended.
    """
    from apprentice.controls.errors import AuthorityError
    from apprentice.core.cycles import end_cycle

    try:
        end_cycle(cycle, outcome, detail)
        output["accounting"] = cycle.summary()
    except AuthorityError as exc:
        output["error"] = f"control authority unavailable: {exc}"
        output["outcome"] = outcome
        return False
    return True


def _cmd_preview(args: Any) -> int:
    from apprentice.core.artifacts import ArtifactError
    from apprentice.core.session_store import SessionStore

    store = SessionStore()
    run_id = args.run_id
    if run_id is None:
        try:
            completed = store.list_runs(status="completed", limit=1)
        except ValueError as exc:
            _print_json({"error": str(exc)})
            return 1
        if not completed:
            _print_json({"error": "No completed run found. Run 'apprentice build' first."})
            return 1
        run_id = completed[0].run_id

    try:
        record = store.load(run_id)
        snapshot = store.load_bundle(record)
    except (FileNotFoundError, ValueError, ArtifactError) as exc:
        _print_json({"error": str(exc), "run_id": run_id})
        return 1

    artifacts = snapshot.describe()
    for entry, artifact in zip(artifacts, snapshot.artifacts, strict=True):
        content = artifact.data.decode("utf-8")
        entry["preview"] = content[:500] + ("..." if len(content) > 500 else "")

    _print_json(
        {
            "run_id": snapshot.run_id,
            "algorithm": snapshot.algorithm,
            "tier": snapshot.tier,
            "manifest_sha256": snapshot.manifest_sha256,
            "bundle_dir": str(store.bundle_dir(snapshot.run_id)),
            "approved": bool(record.approval),
            "artifacts": artifacts,
        }
    )
    return 0


def _cmd_status(cfg: ApprenticeConfig, footprint: Footprint) -> int:
    """Report configured limits, ledger state and route admission separately; admits no work.

    `route.structurally_valid` only says the configured backend, model,
    profile structure and pinned price data validate — not that the profile's
    fees or capabilities are genuine. `route.admissible` additionally
    requires that the ledger blocks nothing for that profile now (no
    quarantine, no unknown current month, no suspension or live legacy
    record); each call is still decided by the budgets.
    """
    from apprentice.controls.errors import AuthorityError
    from apprentice.controls.policy import ControlPolicy
    from apprentice.core.session_store import SessionStore
    from apprentice.metering.pricing import PriceAuthorityError
    from apprentice.metering.profile import ProfileError
    from apprentice.providers.factory import RouteError, resolve_route

    profile_sha256: str | None = None
    try:
        resolved = resolve_route(cfg.provider)
        route: dict[str, Any] = {"structurally_valid": True, **resolved.describe()}
        profile_sha256 = resolved.profile.sha256
    except (RouteError, ProfileError, PriceAuthorityError) as exc:
        route = {"structurally_valid": False, "error": str(exc)}
    store = SessionStore()
    authority = _open_authority(store, footprint)
    if authority is None:
        return 1
    try:
        ledger = authority.status()
        readiness = authority.readiness(profile_sha256, ControlPolicy.from_config(cfg))
        blocked = list(readiness["blocked_by"])
        if not route["structurally_valid"]:
            blocked.insert(0, {"control": "provider", "reason": route["error"]})
        route["admissible"] = not blocked
        route["blocked_by"] = blocked
    except AuthorityError as exc:
        _print_json({"error": f"control authority unavailable: {exc}"})
        return 1
    finally:
        authority.close()
    _print_json(
        {
            "configured": json.loads(ControlPolicy.from_config(cfg).to_json()),
            "ledger": ledger,
            "route": route,
        }
    )
    return 0


def _cmd_controls(footprint: Footprint, args: Any) -> int:
    from apprentice.controls.errors import AuthorityError, ControlDeniedError
    from apprentice.core.session_store import SessionStore

    authority = _open_authority(SessionStore(), footprint)
    if authority is None:
        return 1
    actions = {
        "adopt-legacy": authority.adopt_legacy,
        "reset-circuit": authority.reset_circuit,
        "prepare-rollback": authority.prepare_rollback,
    }
    try:
        result: dict[str, Any] = dict(actions[args.controls_command](args.operator))
    except ControlDeniedError as exc:
        _print_denial(exc)
        return 1
    except AuthorityError as exc:
        _print_json({"error": f"control authority unavailable: {exc}"})
        return 1
    finally:
        authority.close()
    _print_json(result)
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


def _cmd_retry(cfg: ApprenticeConfig, footprint: Footprint, args: Any) -> int:
    """Retry a failed run as a new controlled cycle (not a provider retry)."""
    from apprentice.core.observability import get_logger
    from apprentice.core.session_store import SessionStore

    logger = get_logger(__name__)
    try:
        old_record = SessionStore().load(args.run_id)
    except (FileNotFoundError, ValueError) as exc:
        _print_json({"error": str(exc)})
        return 1

    if old_record.status != "failed":
        _print_json({"error": f"Run {args.run_id} is not failed (status: {old_record.status})"})
        return 1

    algorithm = old_record.algorithm_name
    tier = old_record.tier
    logger.info("retrying: %s (tier %d) from run %s", algorithm, tier, args.run_id)
    result = _controlled_build(cfg, footprint, args, (algorithm, tier, ""), "retry", None)
    if result is None:
        return 1
    if result.outcome != "completed":
        logger.error("retry failed: %s", result.error)
        _print_build_failure(result)
        return 1
    _print_build_result(algorithm, tier, result)
    return 0


def _cmd_history(args: Any) -> int:
    from apprentice.core.session_store import SessionStore

    store = SessionStore()
    try:
        records = store.list_runs(status=args.status, limit=args.limit)
    except ValueError as exc:
        _print_json({"error": str(exc)})
        return 1

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


def _cmd_metrics(footprint: Footprint) -> int:
    """Report run lifecycle and the ledger's usage of every controlled cycle; admits no work."""
    from apprentice.controls.errors import AuthorityError
    from apprentice.core.metrics import aggregate_runs
    from apprentice.core.session_store import SessionStore

    store = SessionStore()
    try:
        records = store.list_runs(limit=None)
    except ValueError as exc:
        _print_json({"error": str(exc)})
        return 1
    authority = _open_authority(store, footprint)
    if authority is None:
        return 1
    try:
        usage = authority.usage()
    except AuthorityError as exc:
        _print_json({"error": f"control authority unavailable: {exc}"})
        return 1
    finally:
        authority.close()
    report = aggregate_runs(records, usage, "installation ledger: every controlled cycle")
    _print_json(report.to_dict())
    return 0


def _print_build_result(algorithm: str, tier: int, result: BuildResult) -> None:
    state = result.session_state
    _print_json(
        {
            "run_id": result.record.run_id,
            "algorithm": algorithm,
            "tier": tier,
            "session_state_keys": list(state.keys()),
            "generated_code": bool(state.get("generated_code")),
            "instrumented_code": bool(state.get("instrumented_code")),
            "manim_scene_code": bool(state.get("manim_scene_code")),
            "anki_deck_content": bool(state.get("anki_deck_content")),
            "review_verdict": state.get("review_verdict", ""),
            "duration_seconds": round(result.elapsed, 2),
            "accounting": result.accounting,
        }
    )


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _get_version() -> str:
    from apprentice import __version__

    return __version__


if __name__ == "__main__":
    sys.exit(main())
