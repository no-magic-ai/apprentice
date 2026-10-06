"""Session persistence — run records and the run-owned artifact roots they govern.

The store is the single authority for run identity and artifact ownership:

    <store_dir>/<run_id>.json            run record
    <store_dir>/runs/<run_id>/work/      mutable generation root of one run
    <store_dir>/runs/<run_id>/bundle/    sealed bundle + manifest (on completion)
    <store_dir>/scratch/<uuid>/          exclusive roots for runs without a record
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apprentice.core.artifacts import (
    ArtifactError,
    BundleSnapshot,
    RunScope,
    load_snapshot,
    require_owned_root,
    seal_bundle,
    state_role_contents,
    tier_directory,
    validate_algorithm_name,
)

_DEFAULT_STORE_DIR = Path.home() / ".apprentice" / "sessions"

_RUN_ID = re.compile(r"[a-z][a-z0-9_]{0,63}-\d{8}T\d{6}Z-[0-9a-f]{32}")
# Records written before run IDs carried a UUID: "<algorithm>-<UTC second>".
_LEGACY_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}-\d{8}T\d{6}Z")


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
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
        )


class SessionStore:
    """Persists run records and allocates the artifact roots each run owns."""

    def __init__(self, store_dir: Path | None = None) -> None:
        self._dir = (store_dir if store_dir is not None else _DEFAULT_STORE_DIR).absolute()
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def store_dir(self) -> Path:
        return self._dir

    def create_run(self, algorithm_name: str, tier: int) -> RunRecord:
        """Create a run with a unique ID, its exclusive work root and an in-progress record."""
        validate_algorithm_name(algorithm_name)
        tier_directory(tier)
        now = datetime.now(tz=UTC)
        run_id = f"{algorithm_name}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}"
        run_dir = self._allocate(self._dir / "runs", run_id)
        (run_dir / "work").mkdir(mode=0o700)
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
        """Allocate a fresh exclusive root for work that has no run record of its own."""
        return self._allocate(self._dir / "scratch", uuid.uuid4().hex)

    def complete_run(
        self,
        record: RunRecord,
        session_state: dict[str, Any],
        budget_summary: dict[str, Any],
        elapsed: float,
    ) -> RunRecord:
        """Seal the run's final artifacts into its bundle and mark it completed."""
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
        """Mark a run as failed, preserving all completed agent outputs."""
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
            ValueError: If `run_id` is not a supported run ID.
            FileNotFoundError: If the run record does not exist.
        """
        path = self._record_path(run_id)
        if not path.exists():
            raise FileNotFoundError(f"No run record found: {run_id}")
        record = RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if record.run_id != run_id:
            raise ValueError(f"run record {path} carries a different run ID {record.run_id!r}")
        return record

    def list_runs(self, status: str | None = None, limit: int = 20) -> list[RunRecord]:
        """List run records, optionally filtered by status, newest `started_at` first."""
        records = [
            RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in self._dir.glob("*.json")
        ]
        records.sort(
            key=lambda record: (datetime.fromisoformat(record.started_at), record.run_id),
            reverse=True,
        )
        matching = [record for record in records if status is None or record.status == status]
        return matching[:limit]

    def save(self, record: RunRecord) -> RunRecord:
        """Persist an updated run record, preserving identity."""
        self._write(record)
        return record

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

    def _run_dir(self, run_id: str) -> Path:
        self._record_path(run_id)
        return self._dir / "runs" / run_id

    def _allocate(self, parent: Path, name: str) -> Path:
        """Create `parent/name` exclusively; an existing root is never reused."""
        parent.mkdir(parents=True, exist_ok=True)
        root = require_owned_root(parent) / name
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            raise ArtifactError(f"artifact root already exists: {root}") from None
        return root

    def _write_new(self, record: RunRecord) -> None:
        path = self._record_path(record.run_id)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), indent=2, default=str))

    def _write(self, record: RunRecord) -> None:
        path = self._record_path(record.run_id)
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        staging.write_text(json.dumps(record.to_dict(), indent=2, default=str), encoding="utf-8")
        staging.replace(path)
