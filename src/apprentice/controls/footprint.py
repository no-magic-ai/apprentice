"""Pre-authority installation footprint and run-record exposure checks.

An installation that ran apprentice before the control authority existed may
have spent tokens without leaving a record (standalone `suggest` and library
calls write none), so its current month cannot start from zero. The footprint
is captured before anything creates the store or log directories: the
resolved store and log roots plus the default `~/.apprentice/sessions` and
`~/.apprentice/logs`, whatever the current configuration says.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from apprentice.controls.errors import AuthorityError
from apprentice.core.artifacts import json_entries

if TYPE_CHECKING:
    from collections.abc import Iterable

_LIVE_SUBMISSION_STATES = frozenset({"pending", "partial"})


@dataclass(frozen=True)
class Footprint:
    """Paths of earlier apprentice state that existed before this process created any."""

    existing: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.existing


def capture_footprint(store_dir: Path, log_dir: Path) -> Footprint:
    """Record which known apprentice state roots already exist; create nothing.

    Raises:
        AuthorityError: If whether a root exists cannot be determined (for
            example a parent directory without search permission); nothing
            is created or changed, since earlier state may be hidden there.
    """
    home = Path.home()
    candidates: Iterable[Path] = (
        store_dir,
        log_dir,
        home / ".apprentice" / "sessions",
        home / ".apprentice" / "logs",
    )
    seen: list[str] = []
    for path in candidates:
        absolute = str(path.expanduser().absolute())
        if absolute in seen:
            continue
        try:
            exists = Path(absolute).exists()
        except OSError as exc:
            raise AuthorityError(
                f"cannot tell whether earlier apprentice state exists at {absolute}: {exc}; "
                "restore the owner's search permission on its parent directories "
                "(nothing was created or changed)"
            ) from exc
        if exists:
            seen.append(absolute)
    return Footprint(existing=tuple(seen))


def parse_utc(value: object, field: str, run_id: str, now: datetime) -> datetime:
    """Parse a stored timezone-aware timestamp that is not in the future."""
    if not isinstance(value, str):
        raise AuthorityError(f"run {run_id}: {field} is not a timestamp string")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AuthorityError(f"run {run_id}: {field} is not ISO 8601: {value!r}") from exc
    if moment.tzinfo is None:
        raise AuthorityError(f"run {run_id}: {field} has no timezone: {value!r}")
    if moment > now:
        raise AuthorityError(f"run {run_id}: {field} is in the future: {value!r}")
    return moment


@dataclass(frozen=True)
class RecordExposure:
    """What one stored run record reveals about past model or publication work."""

    run_id: str
    started_month: str
    live: bool


def scan_records(store_dir: Path, now: datetime) -> list[RecordExposure]:
    """Read every top-level run record and validate its real timestamps.

    A timestamp is in the future only if it is later than the instant its
    record was read (and than the ledger's `now`): a record another process
    wrote after the ledger transaction fixed `now` was written by then.

    Raises:
        AuthorityError: If the store cannot be listed (a record could be
            missed), or a record is unreadable or carries a malformed, naive,
            future or inconsistent `started_at`, `completed_at` or
            `submission.started_at`.
    """
    try:
        paths = json_entries(store_dir)
    except OSError as exc:
        raise AuthorityError(
            f"cannot list run records in {store_dir}: {exc}; restore the owner's read "
            "permission on it (nothing was changed)"
        ) from exc
    exposures: list[RecordExposure] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthorityError(f"cannot read run record {path}: {exc}") from exc
        observed = max(now, datetime.now(tz=UTC))
        if not isinstance(data, dict) or not isinstance(data.get("run_id"), str):
            raise AuthorityError(f"run record {path} has no run_id")
        run_id = data["run_id"]
        started = parse_utc(data.get("started_at"), "started_at", run_id, observed)
        completed = data.get("completed_at", "")
        if completed:
            finished = parse_utc(completed, "completed_at", run_id, observed)
            if finished < started:
                raise AuthorityError(f"run {run_id}: completed_at precedes started_at")
        submission = data.get("submission", {})
        if not isinstance(submission, dict):
            raise AuthorityError(f"run {run_id}: submission is not an object")
        if submission:
            parse_utc(submission.get("started_at"), "submission.started_at", run_id, observed)
        live = data.get("status") == "in_progress" or (
            submission.get("status") in _LIVE_SUBMISSION_STATES
        )
        exposures.append(
            RecordExposure(run_id=run_id, started_month=started.strftime("%Y-%m"), live=live)
        )
    return exposures
