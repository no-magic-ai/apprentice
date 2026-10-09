"""Session persistence — run records and the run-owned artifact roots they govern.

The store is the single authority for run identity and artifact ownership:

    <store_dir>/<run_id>.json            run record
    <store_dir>/runs/<run_id>/work/      mutable generation root of one run
    <store_dir>/runs/<run_id>/bundle/    sealed bundle + manifest (on completion)
    <store_dir>/runs/<run_id>.lock       empty coordination file of `record_lock`
    <store_dir>/scratch/<uuid>/          exclusive roots for runs without a record
"""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apprentice.core.artifacts import (
    ArtifactError,
    BundleSnapshot,
    RunScope,
    _open_single_link_file,
    json_entries,
    load_snapshot,
    require_owned_root,
    seal_bundle,
    state_role_contents,
    tier_directory,
    validate_algorithm_name,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_FAIL = "fail"

_RUN_ID = re.compile(r"[a-z][a-z0-9_]{0,63}-\d{8}T\d{6}Z-[0-9a-f]{32}")
# Records written before run IDs carried a UUID: "<algorithm>-<UTC second>".
_LEGACY_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}-\d{8}T\d{6}Z")
_REQUIRED_TEXT_FIELDS = ("run_id", "algorithm_name", "status", "started_at")


@dataclass
class RunRecord:
    """Persisted record of a single pipeline run.

    Attributes:
        run_id: Unique identifier (algorithm-timestamp-uuid).
        algorithm_name: Algorithm that was built.
        tier: Algorithm tier.
        status: "completed", "failed", or "in_progress".
        session_state: Final ADK session state snapshot.
        budget_summary: Per-agent token/cost breakdown from BudgetTracker.
        started_at: ISO timestamp of run start.
        completed_at: ISO timestamp of run completion (empty if in_progress/failed).
        error: Error message if the run failed.
        elapsed_seconds: Wall-clock duration.
        manifest_sha256: Digest of the sealed bundle manifest (completed runs only).
        approval: Human-review approval bound to the sealed manifest.
        submission: The run's single submit attempt: status (pending, partial,
            failed or complete), manifest digest, workspace, branch, error and
            the repository effects known so far (pushed branches, opened PRs).
    """

    run_id: str
    algorithm_name: str
    tier: int
    status: str
    session_state: dict[str, Any] = field(default_factory=dict)
    budget_summary: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""
    error: str = ""
    elapsed_seconds: float = 0.0
    manifest_sha256: str = ""
    approval: dict[str, Any] = field(default_factory=dict)
    submission: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> RunRecord:
        """Build a record from its stored JSON object.

        Raises:
            ValueError: If `data` is not an object or a required field (run_id,
                algorithm_name, status and started_at as strings, tier as an
                integer) is missing or of the wrong type. Optional fields are
                taken as stored.
        """
        if not isinstance(data, dict):
            raise ValueError(f"run record must be a JSON object, not {type(data).__name__}")
        for key in _REQUIRED_TEXT_FIELDS:
            if not isinstance(data.get(key), str):
                raise ValueError(f"run record field {key!r} must be a string")
        tier = data.get("tier")
        if not isinstance(tier, int) or isinstance(tier, bool):
            raise ValueError("run record field 'tier' must be an integer")
        return cls(
            run_id=data["run_id"],
            algorithm_name=data["algorithm_name"],
            tier=data["tier"],
            status=data["status"],
            session_state=data.get("session_state", {}),
            budget_summary=data.get("budget_summary", {}),
            started_at=data["started_at"],
            completed_at=data.get("completed_at", ""),
            error=data.get("error", ""),
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
            # Absent on records written before sealed bundles existed.
            manifest_sha256=data.get("manifest_sha256", ""),
            approval=data.get("approval", {}),
            submission=data.get("submission", {}),
        )


def blocking_gate_failures(budget_summary: object) -> list[dict[str, Any]]:
    """Return the recorded gate verdicts that failed a blocking gate.

    Verdicts are those `GateAgent` records through `BudgetTracker`. Entries
    written before the `blocking` flag was recorded came from the four
    pipeline gates, which are all blocking, so a missing flag counts as
    blocking — the same default `GateAgent` applies to a gate without one.
    WARN and PASS verdicts never block. A live blocking FAIL already halts
    generation with `BlockingGateError`; this check keeps such a summary from
    being sealed and refuses completed records sealed before that halt existed.

    Raises:
        ValueError: If the summary is not an object, its `gate_verdicts` is not
            a list of objects, or a blocking FAIL lacks a string `gate_name` or
            `after_stage`.
    """
    if not isinstance(budget_summary, dict):
        raise ValueError("budget summary is not an object")
    verdicts = budget_summary.get("gate_verdicts", [])
    if not isinstance(verdicts, list) or not all(isinstance(v, dict) for v in verdicts):
        raise ValueError("budget summary gate_verdicts is not a list of objects")
    failures = [v for v in verdicts if v.get("verdict") == _FAIL and v.get("blocking", True)]
    for failure in failures:
        if not isinstance(failure.get("gate_name"), str) or not isinstance(
            failure.get("after_stage"), str
        ):
            raise ValueError("a failed gate verdict has no gate_name or after_stage")
    return failures


def describe_gate_failures(failures: list[dict[str, Any]]) -> str:
    """Return a one-line description of blocking gate failures."""
    names = ", ".join(f"{f['gate_name']} after {f['after_stage']}" for f in failures)
    return f"blocking gate failed: {names}"


def default_store_dir() -> Path:
    """Return the store root used when none is given: `~/.apprentice/sessions`."""
    return Path.home() / ".apprentice" / "sessions"


class StoreUnavailableError(ArtifactError):
    """Raised when the run-record store root cannot be created or used as a directory."""


class SessionStore:
    """Persists run records and allocates the artifact roots each run owns."""

    def __init__(self, store_dir: Path | None = None) -> None:
        self._dir = (store_dir if store_dir is not None else default_store_dir()).absolute()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Missing permission or a file where the store belongs: nothing is
            # created over it.
            raise StoreUnavailableError(f"run store {self._dir} cannot be used: {exc}") from exc

    @property
    def store_dir(self) -> Path:
        return self._dir

    @staticmethod
    def new_run_id(algorithm_name: str, tier: int) -> str:
        """Validate a run's identity and return a fresh run ID for it; nothing is written.

        A controlled cycle registers the ID before `create_run` writes the
        record, so the record is never seen unreferenced.
        """
        validate_algorithm_name(algorithm_name)
        tier_directory(tier)
        now = datetime.now(tz=UTC)
        return f"{algorithm_name}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}"

    def create_run(self, algorithm_name: str, tier: int, run_id: str | None = None) -> RunRecord:
        """Create a run, its exclusive work root and an in-progress record.

        `run_id` is an ID from `new_run_id` (a fresh one is generated when
        omitted). A record written outside a controlled cycle is unreferenced
        by the control authority, which then holds its month as unknown usage.

        Raises:
            ValueError: If the identity or `run_id` is not valid.
            ArtifactError: If the runs root is not a plain owned directory or
                the run's root already exists.
            StoreUnavailableError: If the run's root or record cannot be created.
        """
        if run_id is None:
            run_id = self.new_run_id(algorithm_name, tier)
        validate_algorithm_name(algorithm_name)
        tier_directory(tier)
        if not _RUN_ID.fullmatch(run_id) or not run_id.startswith(f"{algorithm_name}-"):
            raise ValueError(f"run ID {run_id!r} is not a new run ID of {algorithm_name!r}")
        now = datetime.now(tz=UTC)
        run_dir = self._allocate(self._dir / "runs", run_id)
        work = run_dir / "work"
        try:
            work.mkdir(mode=0o700)
        except OSError as exc:
            raise self._unwritable(f"cannot create {work}", exc) from exc
        record = RunRecord(
            run_id=run_id,
            algorithm_name=algorithm_name,
            tier=tier,
            status="in_progress",
            started_at=now.isoformat(),
        )
        self._write_new(record)
        return record

    def run_scope(self, record: RunRecord) -> RunScope:
        """Return the identity and exclusive work root a generation run writes into."""
        return RunScope(
            run_id=record.run_id,
            algorithm=record.algorithm_name,
            tier=record.tier,
            work_root=require_owned_root(self._run_dir(record.run_id) / "work"),
        )

    def bundle_dir(self, run_id: str) -> Path:
        """Return where the sealed bundle of `run_id` lives (it may not exist)."""
        return self._run_dir(run_id) / "bundle"

    def allocate_work_root(self) -> Path:
        """Allocate a fresh exclusive root for work that has no run record of its own.

        Raises:
            ArtifactError: If the scratch root is not a plain owned directory.
            StoreUnavailableError: If the root cannot be created.
        """
        return self._allocate(self._dir / "scratch", uuid.uuid4().hex)

    def complete_run(
        self,
        record: RunRecord,
        session_state: dict[str, Any],
        budget_summary: dict[str, Any],
        elapsed: float,
    ) -> RunRecord:
        """Seal the run's final artifacts into its bundle and mark it completed.

        Raises:
            ArtifactError: If a blocking gate failed (such a run is never sealed)
                or the gate verdicts in `budget_summary` are malformed.
            StoreUnavailableError: If the store cannot be written (the record
                keeps its previous state).
        """
        try:
            failures = blocking_gate_failures(budget_summary)
        except ValueError as exc:
            raise ArtifactError(f"run {record.run_id} cannot be sealed: {exc}") from exc
        if failures:
            raise ArtifactError(
                f"run {record.run_id} cannot be sealed: {describe_gate_failures(failures)}"
            )
        record.manifest_sha256 = seal_bundle(
            self.bundle_dir(record.run_id),
            run_id=record.run_id,
            algorithm=record.algorithm_name,
            tier=record.tier,
            contents=state_role_contents(session_state),
        )
        record.status = "completed"
        record.session_state = session_state
        record.budget_summary = budget_summary
        record.completed_at = datetime.now(tz=UTC).isoformat()
        record.elapsed_seconds = elapsed
        self._write(record)
        return record

    def fail_run(
        self,
        record: RunRecord,
        session_state: dict[str, Any],
        budget_summary: dict[str, Any],
        elapsed: float,
        error: str,
    ) -> RunRecord:
        """Mark a run as failed, preserving all completed agent outputs.

        Raises:
            StoreUnavailableError: If the store cannot be written (the record
                keeps its previous state).
        """
        record.status = "failed"
        record.session_state = session_state
        record.budget_summary = budget_summary
        record.completed_at = datetime.now(tz=UTC).isoformat()
        record.elapsed_seconds = elapsed
        record.error = error
        self._write(record)
        return record

    def load_bundle(self, record: RunRecord) -> BundleSnapshot:
        """Verify and capture the sealed bundle of a completed run.

        Raises:
            ArtifactError: If the run has no sealed bundle (including runs that
                predate sealed bundles), or the bundle fails verification or
                does not match the run's identity and completion digest.
        """
        bundle_dir = self.bundle_dir(record.run_id)
        if not record.manifest_sha256 or not os.path.lexists(bundle_dir):
            raise ArtifactError(
                f"run {record.run_id} has no sealed artifact bundle; rebuild with "
                f"`apprentice build {record.algorithm_name} --tier {record.tier}` "
                "and review the new run"
            )
        snapshot = load_snapshot(bundle_dir)
        if snapshot.manifest_sha256 != record.manifest_sha256:
            raise ArtifactError(
                f"bundle manifest of run {record.run_id} differs from the one sealed at completion"
            )
        if (snapshot.run_id, snapshot.algorithm, snapshot.tier) != (
            record.run_id,
            record.algorithm_name,
            record.tier,
        ):
            raise ArtifactError(f"bundle identity does not match run {record.run_id}")
        return snapshot

    def load(self, run_id: str) -> RunRecord:
        """Load a run record by its exact ID.

        Raises:
            ValueError: If `run_id` is not a supported run ID or the stored
                record is not a valid run record.
            FileNotFoundError: If the run record does not exist.
            StoreUnavailableError: If whether it exists cannot be determined or
                it cannot be read.
        """
        path = self._existing_record_path(run_id)
        record = _read_record(path)
        if record.run_id != run_id:
            raise ValueError(f"run record {path} carries a different run ID {record.run_id!r}")
        return record

    def list_runs(self, status: str | None = None, limit: int | None = 20) -> list[RunRecord]:
        """List run records, optionally filtered by status, newest `started_at` first.

        `limit=None` lists every record.

        Raises:
            ValueError: If any stored record is not a valid run record or its
                `started_at` is not an ISO 8601 timestamp; no record is skipped.
            StoreUnavailableError: If the store cannot be listed or any stored
                record cannot be read.
        """
        try:
            paths = json_entries(self._dir)
        except OSError as exc:
            raise StoreUnavailableError(
                f"run store {self._dir} cannot be used: cannot list its run records: {exc}; "
                "restore the owner's read permission on it (nothing was changed)"
            ) from exc
        keyed = []
        for path in paths:
            record = _read_record(path)
            try:
                started = datetime.fromisoformat(record.started_at)
            except ValueError as exc:
                raise ValueError(f"corrupt run record {path}: {exc}") from exc
            keyed.append(((started, record.run_id), record))
        keyed.sort(key=lambda item: item[0], reverse=True)
        matching = [record for _, record in keyed if status is None or record.status == status]
        return matching[:limit]

    def save(self, record: RunRecord) -> RunRecord:
        """Persist an updated run record, preserving identity.

        Raises:
            StoreUnavailableError: If the store cannot be written (the record
                keeps its previous state).
        """
        self._write(record)
        return record

    @contextmanager
    def record_lock(self, run_id: str) -> Iterator[None]:
        """Hold the exclusive lock that `approve` and `submit` take on one run.

        The lock is an advisory `flock` on `runs/<run_id>.lock`, a dedicated
        empty file that is created once and never replaced or removed, so every
        holder locks the same inode. It serializes reading, checking and saving
        the run record; it is held only for local work (no network, subprocess
        or model call). Other readers and writers of the record do not take it.

        Raises:
            ValueError: If `run_id` is not a supported run ID.
            FileNotFoundError: If the run has no record (no lock file is created).
            StoreUnavailableError: If whether the record exists cannot be
                determined, or the runs root cannot be created (no lock file
                is created).
            ArtifactError: If the lock path is a symlink, not a regular file or
                has more than one link.
        """
        import fcntl

        self._existing_record_path(run_id)
        runs = self._dir / "runs"
        try:
            runs.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._unwritable(f"cannot create {runs}", exc) from exc
        path = require_owned_root(runs) / f"{run_id}.lock"
        fd = _open_single_link_file(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def delete(self, run_id: str) -> bool:
        """Delete a run record. Returns True if deleted, False if not found."""
        path = self._record_path(run_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def _record_path(self, run_id: str) -> Path:
        if not (_RUN_ID.fullmatch(run_id) or _LEGACY_RUN_ID.fullmatch(run_id)):
            raise ValueError(f"invalid run ID {run_id!r}")
        return self._dir / f"{run_id}.json"

    def _existing_record_path(self, run_id: str) -> Path:
        """Return the path of `run_id`'s record, which must exist.

        Raises:
            ValueError: If `run_id` is not a supported run ID.
            FileNotFoundError: If the run has no record.
            StoreUnavailableError: If whether the record exists cannot be
                determined (for example a store root without search
                permission); nothing is changed.
        """
        path = self._record_path(run_id)
        try:
            exists = path.exists()
        except OSError as exc:
            raise StoreUnavailableError(
                f"run store {self._dir} cannot be used: cannot check run record {path}: {exc}; "
                "restore the owner's search permission on it (nothing was changed)"
            ) from exc
        if not exists:
            raise FileNotFoundError(f"No run record found: {run_id}")
        return path

    def _run_dir(self, run_id: str) -> Path:
        self._record_path(run_id)
        return self._dir / "runs" / run_id

    def _allocate(self, parent: Path, name: str) -> Path:
        """Create `parent/name` exclusively; an existing root is never reused."""
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._unwritable(f"cannot create {parent}", exc) from exc
        root = require_owned_root(parent) / name
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            raise ArtifactError(f"artifact root already exists: {root}") from None
        except OSError as exc:
            raise self._unwritable(f"cannot create {root}", exc) from exc
        return root

    def _write_new(self, record: RunRecord) -> None:
        path = self._record_path(record.run_id)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), indent=2, default=str))
        except FileExistsError:
            raise ArtifactError(f"run record already exists: {path}") from None
        except OSError as exc:
            raise self._unwritable(f"cannot write run record {path}", exc) from exc

    def _write(self, record: RunRecord) -> None:
        """Replace the stored record atomically; on failure the stored record is unchanged."""
        path = self._record_path(record.run_id)
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        text = json.dumps(record.to_dict(), indent=2, default=str)
        try:
            staging.write_text(text, encoding="utf-8")
            staging.replace(path)
        except OSError as exc:
            raise self._unwritable(f"cannot write run record {path}", exc) from exc

    def _unwritable(self, action: str, exc: OSError) -> StoreUnavailableError:
        """The typed failure of a write into this store (permission, missing or misplaced path)."""
        return StoreUnavailableError(
            f"run store {self._dir} cannot be used: {action}: {exc}; restore the owner's write "
            "permission on it (a stored run record keeps its previous state)"
        )


def _read_record(path: Path) -> RunRecord:
    """Parse one stored run record, naming the file if it is not a valid record.

    Raises:
        ValueError: If the record is not a valid run record.
        FileNotFoundError: If the record does not exist.
        StoreUnavailableError: If the record cannot be read (for example no
            read permission); nothing is changed.
    """
    try:
        return RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise ValueError(f"corrupt run record {path}: {exc}") from exc
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StoreUnavailableError(
            f"run store {path.parent} cannot be used: cannot read run record {path}: {exc}; "
            "restore the owner's read permission on it (nothing was changed)"
        ) from exc
