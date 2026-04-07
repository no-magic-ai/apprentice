"""Session persistence — save and load ADK pipeline run state to disk."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DEFAULT_STORE_DIR = Path.home() / ".apprentice" / "sessions"


@dataclass
class RunRecord:
    """Persisted record of a single pipeline run.

    Attributes:
        run_id: Unique identifier (algorithm-timestamp).
        algorithm_name: Algorithm that was built.
        tier: Algorithm tier.
        status: "completed", "failed", or "in_progress".
        session_state: Final ADK session state snapshot.
        budget_summary: Per-agent token/cost breakdown from BudgetTracker.
        started_at: ISO timestamp of run start.
        completed_at: ISO timestamp of run completion (empty if in_progress/failed).
        error: Error message if the run failed.
        elapsed_seconds: Wall-clock duration.
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
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            error=data.get("error", ""),
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
        )


class SessionStore:
    """Persists pipeline run records as JSON files on disk.

    Each run is stored as a separate JSON file named by run_id.
    """

    def __init__(self, store_dir: Path | None = None) -> None:
        self._dir = store_dir if store_dir is not None else _DEFAULT_STORE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def store_dir(self) -> Path:
        return self._dir

    def create_run(self, algorithm_name: str, tier: int) -> RunRecord:
        """Create and persist a new run record in "in_progress" state."""
        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{algorithm_name}-{ts}"
        record = RunRecord(
            run_id=run_id,
            algorithm_name=algorithm_name,
            tier=tier,
            status="in_progress",
            started_at=datetime.now(tz=UTC).isoformat(),
        )
        self._write(record)
        return record

    def complete_run(
        self,
        record: RunRecord,
        session_state: dict[str, Any],
        budget_summary: dict[str, Any],
        elapsed: float,
    ) -> RunRecord:
        """Mark a run as completed with final state and metrics."""
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

    def load(self, run_id: str) -> RunRecord:
        """Load a run record by ID.

        Raises:
            FileNotFoundError: If the run record does not exist.
        """
        path = self._path_for(run_id)
        if not path.exists():
            raise FileNotFoundError(f"No run record found: {run_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunRecord.from_dict(data)

    def list_runs(self, status: str | None = None, limit: int = 20) -> list[RunRecord]:
        """List run records, optionally filtered by status, newest first."""
        records: list[RunRecord] = []
        json_files = sorted(self._dir.glob("*.json"), reverse=True)
        for path in json_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = RunRecord.from_dict(data)
            if status is None or record.status == status:
                records.append(record)
            if len(records) >= limit:
                break
        return records

    def delete(self, run_id: str) -> bool:
        """Delete a run record. Returns True if deleted, False if not found."""
        path = self._path_for(run_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def _path_for(self, run_id: str) -> Path:
        safe_id = run_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe_id}.json"

    def _write(self, record: RunRecord) -> None:
        path = self._path_for(record.run_id)
        path.write_text(
            json.dumps(record.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
