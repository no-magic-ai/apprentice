"""Controlled work cycles: one admitted, metered and recorded unit of factory work.

Every model-using entry point (CLI build/retry/suggest and library use such
as `scripts/integration_test.py`) opens the installation authority with the
footprint captured before any state was created, resolves its route before
admission, and runs inside one cycle. The cycle's terminal outcome is
committed before its lease is released:

- `completed`: the run produced and sealed its output;
- `failed`: a blocking gate, a transport/usage-contract error or any other
  error ended the admitted work;
- `denied`: a control refused a model call before it was sent (neutral);
- `cancelled`: the user interrupted the work (neutral).

An operator SIGTERM to the CLI (`operator_stop`) interrupts only the model
work itself (`interruptible`). While a cycle's admission or end records are
being committed (`recording_end`) it is deferred and raised once they are,
so a stop never leaves a cycle active for recovery to settle as owner-lost.
The stop then reports the outcome the cycle reached (`end_cycle`,
`reached_end`), never one it did not commit.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apprentice.controls.authority import Authority
from apprentice.controls.errors import AuthorityError, ControlDeniedError
from apprentice.controls.footprint import Footprint, capture_footprint
from apprentice.core.artifacts import ArtifactError
from apprentice.core.budget import BudgetTracker
from apprentice.core.session_store import SessionStore, StoreUnavailableError, default_store_dir
from apprentice.models.work_item import BlockingGateError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from apprentice.controls.authority import Cycle
    from apprentice.controls.policy import ControlPolicy
    from apprentice.core.config import ApprenticeConfig
    from apprentice.core.session_store import RunRecord
    from apprentice.providers.factory import ModelRoute


def log_dir(config: ApprenticeConfig) -> Path:
    """Return the configured log root exactly as `setup_logging` resolves it."""
    import os

    return Path(os.path.expandvars(config.observability.log_path))


def capture_installation_footprint(config: ApprenticeConfig, store_dir: Path | None) -> Footprint:
    """Capture earlier apprentice state before logging or the store creates anything."""
    return capture_footprint(
        store_dir if store_dir is not None else default_store_dir(), log_dir(config)
    )


def bootstrap_installation(config: ApprenticeConfig, store_dir: Path | None) -> Footprint:
    """Capture the earlier footprint; on a fresh installation create the authority now.

    Must run before logging is set up. When no earlier apprentice state
    exists, the store root and the authority are created here, from that
    empty footprint, so a first command that admits no work (or is refused)
    cannot leave its own logs to be mistaken for earlier, unmetered use.
    When earlier state exists, the authority is created by the first command
    that opens it — the current month is held as unknown either way — and
    commands that only read records keep their own diagnostics.

    Raises:
        AuthorityError: If the earlier footprint cannot be determined or the
            controls directory is in an untrustworthy state.
        ArtifactError: If the store root is not a plain owned directory.
    """
    footprint = capture_installation_footprint(config, store_dir)
    if footprint.empty:
        store = SessionStore(store_dir)
        Authority.ensure_installation(store.store_dir, footprint)
    return footprint


def open_authority(store: SessionStore, footprint: Footprint) -> Authority:
    """Open the control authority of `store` (see `Authority.open`)."""
    return Authority.open(store.store_dir, footprint)


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def classify(exc: BaseException) -> str:
    """Return the cycle outcome an exception that ended admitted work implies."""
    leaves = _leaves(exc)
    if any(isinstance(leaf, (KeyboardInterrupt, asyncio.CancelledError)) for leaf in leaves):
        return "cancelled"
    if all(isinstance(leaf, ControlDeniedError) for leaf in leaves):
        return "denied"
    return "failed"


def describe_error(exc: BaseException) -> str:
    """Describe what ended admitted work; a broken authority is named as such."""
    failures = [leaf for leaf in _leaves(exc) if isinstance(leaf, AuthorityError)]
    if failures:
        return f"control authority unavailable: {failures[0]}"
    return "; ".join(str(leaf) or repr(leaf) for leaf in _leaves(exc))


class OperatorTermination(KeyboardInterrupt):
    """SIGTERM delivered to a CLI process: an explicit operator cancellation.

    A subclass of KeyboardInterrupt so admitted work ends exactly as for
    Ctrl-C: dispatched calls keep their full bound held as unknown and the
    cycle ends `cancelled` (neutral), unlike an uncontrolled owner loss.
    """


class _OperatorStops:
    """Whether an operator stop is raised at once or after the cycle's records.

    `reached` is the terminal state the current command's last cycle
    committed, which an operator stop reports instead of claiming one;
    `notes` are what the records being committed could not save, added to a
    stop raised when they are done.
    """

    deferred = False
    pending = False
    reached: dict[str, str] | None = None
    notes: tuple[str, ...] = ()


_STOPS = _OperatorStops()


def operator_stop() -> None:
    """Act on an operator SIGTERM (the CLI's handler): stop now, or once records are committed.

    Raises:
        OperatorTermination: Unless a cycle's records are being committed.
    """
    if _STOPS.deferred:
        _STOPS.pending = True
        return
    raise OperatorTermination


def clear_operator_stop() -> None:
    """Forget a pending stop and the last cycle's end (start and end of a CLI command)."""
    _STOPS.pending = False
    _STOPS.reached = None
    _STOPS.notes = ()


def end_cycle(cycle: Cycle, outcome: str, detail: str = "") -> None:
    """Commit `cycle`'s terminal outcome and note it for an operator stop to report.

    Raises:
        AuthorityError: If the terminal state cannot be committed (nothing is noted).
    """
    if cycle.finished:
        return
    cycle.finish(outcome, detail)
    reached = {"outcome": outcome, "cycle_id": cycle.cycle_id}
    if cycle.run_id is not None:
        reached["run_id"] = cycle.run_id
    _STOPS.reached = reached


def reached_end() -> dict[str, str]:
    """The terminal outcome and IDs the current command's last cycle committed, if any."""
    return dict(_STOPS.reached or {})


@contextmanager
def recording_end() -> Iterator[None]:
    """Commit a cycle's admission and end without an operator stop splitting them.

    A stop delivered meanwhile is raised as `OperatorTermination` when the
    block completes, after the run record and the cycle's terminal state, with
    what those records could not save (`note_for_stop`) as its notes.
    """
    previous = _STOPS.deferred
    if not previous:
        _STOPS.notes = ()
    _STOPS.deferred = True
    try:
        yield
    finally:
        _STOPS.deferred = previous
    if _STOPS.pending and not previous:
        _STOPS.pending = False
        stop = OperatorTermination()
        for note in _STOPS.notes:
            stop.add_note(note)
        raise stop


def note_for_stop(note: str) -> None:
    """Record what the current `recording_end` block could not save, for a deferred stop."""
    _STOPS.notes = (*_STOPS.notes, note)


@contextmanager
def interruptible() -> Iterator[None]:
    """Let an operator stop interrupt the model work in this block (a deferred one first)."""
    previous = _STOPS.deferred
    _STOPS.deferred = False
    try:
        if _STOPS.pending:
            _STOPS.pending = False
            raise OperatorTermination
        yield
    finally:
        _STOPS.deferred = previous


def denied_controls(exc: BaseException) -> list[str]:
    """Return the controls that refused calls inside the work `exc` ended."""
    return [leaf.control for leaf in _leaves(exc) if isinstance(leaf, ControlDeniedError)]


@dataclass
class BuildResult:
    """What one controlled build cycle produced and how it ended."""

    record: RunRecord
    session_state: dict[str, Any]
    elapsed: float
    outcome: str
    error: str = ""
    gate: dict[str, Any] | None = None
    controls: list[str] = field(default_factory=list)
    accounting: dict[str, Any] = field(default_factory=dict)


def run_build(
    *,
    store: SessionStore,
    authority: Authority,
    route: ModelRoute,
    policy: ControlPolicy,
    request: tuple[str, int, str],
    kind: str,
    progress: Any = None,
) -> BuildResult:
    """Admit one build cycle, run the pipeline in it and record the run.

    `request` is (algorithm, tier, description). The run ID is registered
    with the cycle before its record is written.

    Raises:
        ControlDeniedError: If the cycle itself is not admitted (no run is created).
        StoreUnavailableError: If the run's record or work root cannot be
            created (its cycle has already ended `failed`).
        KeyboardInterrupt: After recording a cancelled run and cycle; an
            operator stop that arrives while the end is being recorded is
            raised after it (the run and cycle keep the outcome they reached).
            If the run record cannot be written, the cycle still ends with the
            outcome it reached and the reason is added as a note to the
            interruption (or to the result's `error`).
    """
    from apprentice.core.orchestrator import build_pipeline

    algorithm, tier, description = request
    run_id = SessionStore.new_run_id(algorithm, tier)
    with recording_end():
        cycle = authority.begin_cycle(kind, policy, run_id=run_id)
        try:
            record = store.create_run(algorithm, tier, run_id=run_id)
        except BaseException as exc:
            end_cycle(cycle, classify(exc), describe_error(exc))
            if isinstance(exc, StoreUnavailableError):
                raise StoreUnavailableError(
                    f"{exc}; run {run_id} was not created and its cycle ended {classify(exc)}"
                ) from exc
            if isinstance(exc, ArtifactError):
                # The runs root is not a plain owned directory (for example a symlink).
                raise StoreUnavailableError(
                    f"run store {store.store_dir} cannot be used: run {run_id} could not be "
                    f"created ({exc}); its cycle ended {classify(exc)}"
                ) from exc
            raise
        tracker = BudgetTracker()

        def snapshot() -> dict[str, Any]:
            # The run record keeps a derived copy of the cycle's ledger rows; if
            # the ledger cannot be read the record says so instead of guessing.
            try:
                return tracker.to_dict(cycle.summary())
            except AuthorityError as exc:
                return tracker.to_dict({"unavailable": f"control authority unavailable: {exc}"})

        start = time.monotonic()
        state: dict[str, Any] = {}
        gate: dict[str, Any] | None = None
        controls: list[str] = []
        try:
            pipeline = build_pipeline(route, cycle, store.run_scope(record), tracker)
            with interruptible():
                state = asyncio.run(_run_pipeline(pipeline, algorithm, tier, description, progress))
            outcome, error = (
                ("completed", "")
                if state.get("generated_code")
                else (
                    "failed",
                    "no output generated",
                )
            )
        except BlockingGateError as failure:
            state, gate = failure.persisted_state(), failure.verdict
            outcome, error = "failed", str(failure)
        except BaseException as exc:
            outcome, error, controls = classify(exc), describe_error(exc), denied_controls(exc)
            if outcome == "cancelled":
                elapsed = time.monotonic() - start
                unsaved = _record_end(
                    lambda: store.fail_run(record, state, snapshot(), elapsed, "cancelled"), record
                )
                end_cycle(cycle, "cancelled", "; ".join(filter(None, (error, unsaved))))
                if unsaved:
                    exc.add_note(unsaved)
                raise
        elapsed = time.monotonic() - start
        if outcome == "completed":
            try:
                store.complete_run(record, state, snapshot(), elapsed)
            except Exception as exc:
                outcome, error = "failed", str(exc)
        if outcome != "completed":
            unsaved = _record_end(
                lambda: store.fail_run(record, state, snapshot(), elapsed, error), record
            )
            if unsaved:
                note_for_stop(unsaved)
            error = "; ".join(filter(None, (error, unsaved)))
        end_cycle(cycle, outcome, error)
        return BuildResult(
            record=record,
            session_state=state,
            elapsed=elapsed,
            outcome=outcome,
            error=error,
            gate=gate,
            controls=controls,
            accounting=snapshot()["accounting"],
        )


def _record_end(write: Callable[[], object], record: RunRecord) -> str:
    """Write a run record's end; return why it could not be saved, or "".

    The cycle's terminal state is committed either way: a run record that
    cannot be written (the store's typed write failure) must not leave the
    cycle active for recovery to settle as owner-lost. The record on disk then
    keeps its previous state, and the returned reason says so.
    """
    try:
        write()
    except StoreUnavailableError as exc:
        return f"run record {record.run_id} could not be saved (it keeps its previous state): {exc}"
    return ""


async def _run_pipeline(
    pipeline: Any,
    algorithm: str,
    tier: int,
    description: str,
    progress: Any,
) -> dict[str, Any]:
    """Run the ADK pipeline via Runner (non-streaming) with optional progress tracking."""
    from google.adk.agents import RunConfig
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    session_service = InMemorySessionService()  # type: ignore[no-untyped-call]
    runner = Runner(
        agent=pipeline,
        app_name="apprentice",
        session_service=session_service,
        artifact_service=InMemoryArtifactService(),
    )
    user_id = "cli"
    session = await session_service.create_session(
        app_name="apprentice",
        user_id=user_id,
        state={"algorithm_name": algorithm, "algorithm_tier": tier, "description": description},
    )
    user_message = types.Content(
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
    run_config = RunConfig(max_llm_calls=50)
    try:
        if progress is not None:
            with progress.start():
                async for event in runner.run_async(
                    user_id=user_id,
                    session_id=session.id,
                    new_message=user_message,
                    run_config=run_config,
                ):
                    progress.on_event(event)
        else:
            async for _event in runner.run_async(
                user_id=user_id,
                session_id=session.id,
                new_message=user_message,
                run_config=run_config,
            ):
                pass
    except BlockingGateError as failure:
        # The gate's verdict delta is already stored; read the session back
        # from this same service so the halted run keeps its outputs.
        stored = await session_service.get_session(
            app_name="apprentice", user_id=user_id, session_id=session.id
        )
        if stored is None:
            raise RuntimeError(f"session {session.id} vanished after {failure}") from failure
        failure.session_state = dict(stored.state)
        raise
    updated = await session_service.get_session(
        app_name="apprentice", user_id=user_id, session_id=session.id
    )
    return dict(updated.state) if updated else {}


async def run_agent(agent: Any, prompt: str) -> dict[str, Any]:
    """Run a single ADK agent via Runner (non-streaming) and return session state."""
    from google.adk.agents import RunConfig
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    session_service = InMemorySessionService()  # type: ignore[no-untyped-call]
    runner = Runner(
        agent=agent,
        app_name="apprentice",
        session_service=session_service,
        artifact_service=InMemoryArtifactService(),
    )
    session = await session_service.create_session(app_name="apprentice", user_id="cli")
    async for _event in runner.run_async(
        user_id="cli",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        run_config=RunConfig(max_llm_calls=30),
    ):
        pass
    updated = await session_service.get_session(
        app_name="apprentice", user_id="cli", session_id=session.id
    )
    return dict(updated.state) if updated else {}
