"""The installation control authority: one SQLite ledger plus OS cycle leases.

Layout under the resolved SessionStore root (outside the `*.json` run-record
glob):

    controls/authority.id         stable identity marker (plain text, not JSON)
    controls/accounting.sqlite3   versioned ledger; every decision is one
                                  `BEGIN IMMEDIATE` transaction
    controls/leases/<slot>.lock   reusable OS `flock` leases of live cycles

A cycle takes the lowest free slot's lease before its active row is
committed and releases it only after its terminal state is committed. A
holder that died released its lease with its process, so a nonblocking
`flock` on the same inode proves owner loss — never a PID or timeout guess.
Recovery keeps every dispatched-but-unsettled bound as unknown exposure.

A missing database beside an existing marker (or the reverse), a foreign
marker, an unsupported schema, a corrupt database or a clock that runs
backwards are fatal: nothing is admitted and nothing is reinitialized.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apprentice.controls.errors import AuthorityError, ControlDeniedError
from apprentice.controls.footprint import Footprint, scan_records
from apprentice.controls.policy import ControlPolicy
from apprentice.core.artifacts import ArtifactError, _open_single_link_file, require_owned_root

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SCHEMA_VERSION = 1
CONTROLS_DIR = "controls"
MARKER = "authority.id"
DATABASE = "accounting.sqlite3"
LEASES = "leases"

ACTIVE = "active"
TERMINAL = "terminal"
OWNER_LOST = "owner-lost"

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE cycles (
    cycle_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    run_id TEXT,
    slot INTEGER NOT NULL,
    admitted_at TEXT NOT NULL,
    policy TEXT NOT NULL,
    state TEXT NOT NULL,
    outcome TEXT,
    detail TEXT,
    finished_at TEXT
);
CREATE INDEX cycles_state ON cycles (state);
CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES cycles (cycle_id),
    attempt_id TEXT NOT NULL,
    month TEXT NOT NULL,
    stage TEXT NOT NULL,
    role TEXT NOT NULL,
    operation TEXT NOT NULL,
    basis TEXT NOT NULL,
    profile_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    bound_tokens INTEGER NOT NULL,
    bound_nanodollars INTEGER NOT NULL,
    charged_tokens INTEGER,
    charged_nanodollars INTEGER,
    receipt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX entries_month ON entries (month);
CREATE INDEX entries_cycle ON entries (cycle_id);
CREATE TABLE quarantine (
    profile_sha256 TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    entry_id TEXT,
    at TEXT NOT NULL
);
CREATE TABLE unknown_months (month TEXT PRIMARY KEY, reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE known_records (
    run_id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    live INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    adopted_at TEXT,
    adopted_by TEXT
);
"""

# Ledger states. An entry is an active refundable hold while reserved or
# dispatched; settled entries count their charge, unknown ones their bound.
RESERVED = "reserved"
DISPATCHED = "dispatched"
SETTLED = "settled"
UNKNOWN = "unknown"
RELEASED = "released"


_CONTINUITY = frozenset({"continuous", "suspended"})


def _expected_columns() -> dict[str, list[tuple[str, str, int]]]:
    """Column (name, type, not-null) lists of every table this schema version defines."""
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(_SCHEMA)
        tables = [
            r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            table: [(c[1], c[2], c[3]) for c in probe.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }
    finally:
        probe.close()


_COLUMNS = _expected_columns()


def _meta_value(conn: sqlite3.Connection, key: str) -> str:
    """A required ledger metadata value; its absence is damage, never a default."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None or not isinstance(row[0], str):
        raise AuthorityError(f"control ledger is damaged: required metadata {key!r} is missing")
    return row[0]


def _meta_instant(conn: sqlite3.Connection, key: str) -> datetime:
    value = _meta_value(conn, key)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AuthorityError(
            f"control ledger is damaged: {key} {value!r} is not a timestamp"
        ) from exc
    if moment.tzinfo is None:
        raise AuthorityError(f"control ledger is damaged: {key} {value!r} has no timezone")
    return moment


def _stored_policy(conn: sqlite3.Connection) -> ControlPolicy | None:
    """The persisted effective policy, or None before the first cycle was ever admitted."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'policy'").fetchone()
    if row is None:
        (cycles,) = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()
        if cycles:
            raise AuthorityError("control ledger is damaged: the effective policy is missing")
        return None
    try:
        return ControlPolicy.from_json(row[0])
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise AuthorityError(
            f"control ledger is damaged: the effective policy is unreadable: {exc}"
        ) from exc


def _validate_ledger(conn: sqlite3.Connection) -> None:
    """Require the exact supported structure and readable required metadata.

    Raises:
        AuthorityError: On page corruption, a missing or altered table or
            column, or missing/garbled required metadata. Nothing is repaired.
    """
    (check,) = conn.execute("PRAGMA quick_check").fetchone()
    if check != "ok":
        raise AuthorityError(f"control ledger is damaged: integrity check reports {check!r}")
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table, expected in _COLUMNS.items():
        if table not in present:
            raise AuthorityError(f"control ledger is damaged: table {table!r} is missing")
        actual = [(c[1], c[2], c[3]) for c in conn.execute(f"PRAGMA table_info({table})")]
        if actual != expected:
            raise AuthorityError(f"control ledger is damaged: table {table!r} has columns {actual}")
    _meta_instant(conn, "created_at")
    _meta_instant(conn, "last_clock")
    continuity = _meta_value(conn, "continuity")
    if continuity not in _CONTINUITY:
        raise AuthorityError(f"control ledger is damaged: continuity {continuity!r} is unknown")
    try:
        footprint = json.loads(_meta_value(conn, "footprint"))
    except ValueError as exc:
        raise AuthorityError("control ledger is damaged: the footprint is not JSON") from exc
    if not isinstance(footprint, list) or not all(isinstance(p, str) for p in footprint):
        raise AuthorityError("control ledger is damaged: the footprint is not a list of paths")
    _stored_policy(conn)


def utc_month(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m")


def _require_regular(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AuthorityError(f"{path} is not a single-link regular file")


def _owned_dir(path: Path) -> Path:
    try:
        return require_owned_root(path)
    except ArtifactError as exc:
        raise AuthorityError(str(exc)) from exc


@dataclass(frozen=True)
class LedgerUsage:
    """Admitted cycles and their ledger entries from one ledger read (`Authority.usage`)."""

    cycles: list[dict[str, Any]]
    entries: list[dict[str, Any]]


class Authority:
    """Open handle on the installation's control authority."""

    def __init__(self, store_dir: Path, conn: sqlite3.Connection, authority_id: str) -> None:
        self.store_dir = store_dir
        self.controls = store_dir / CONTROLS_DIR
        self.authority_id = authority_id
        self._conn = conn

    @classmethod
    def open(cls, store_dir: Path, footprint: Footprint) -> Authority:
        """Open the authority of the store at `store_dir`, creating it if absent.

        A new authority starts from a zero ledger only when `footprint` (taken
        before this process created any state) is empty. Otherwise the current
        UTC month is held as unknown exposure and existing run records are
        registered as legacy; live legacy records need `adopt_legacy`.

        Raises:
            AuthorityError: On partial, foreign, corrupt or unsupported state.
        """
        with cls._guarded(store_dir) as (store, controls):
            marker = controls / MARKER
            database = controls / DATABASE
            has_marker = os.path.lexists(marker)
            has_database = os.path.lexists(database)
            if has_marker != has_database:
                present = MARKER if has_marker else DATABASE
                raise AuthorityError(
                    f"control authority at {controls} is partial: only {present} exists; "
                    "restore the missing file from backup — it is never recreated"
                )
            if not has_marker:
                cls._bootstrap(store, controls, footprint)
            _require_regular(marker)
            _require_regular(database)
            try:
                authority_id = marker.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise AuthorityError(
                    f"control authority marker {marker} is unreadable: {exc}"
                ) from exc
            try:
                conn = sqlite3.connect(database, isolation_level=None, timeout=60)
            except sqlite3.DatabaseError as exc:
                raise AuthorityError(f"control ledger {database} cannot be opened: {exc}") from exc
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA synchronous = FULL")
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            except sqlite3.DatabaseError as exc:
                conn.close()
                raise AuthorityError(f"control ledger {database} is unreadable: {exc}") from exc
            if meta.get("schema_version") != str(SCHEMA_VERSION):
                conn.close()
                raise AuthorityError(
                    f"control ledger {database} has unsupported schema "
                    f"{meta.get('schema_version')!r}; this version reads {SCHEMA_VERSION}"
                )
            if meta.get("authority_id") != authority_id:
                conn.close()
                raise AuthorityError(f"control ledger {database} belongs to another authority")
            try:
                _validate_ledger(conn)
            except sqlite3.DatabaseError as exc:
                conn.close()
                raise AuthorityError(f"control ledger {database} is unreadable: {exc}") from exc
            except AuthorityError as exc:
                conn.close()
                raise AuthorityError(f"{exc} ({database}); restore it from backup") from exc
            return cls(store, conn, authority_id)

    @classmethod
    def ensure_installation(cls, store_dir: Path, footprint: Footprint) -> None:
        """Create the installation's authority from `footprint` if none exists yet.

        Called before the process creates its own log or other state, so a
        first command that admits no work still records the installation's
        real pre-existing footprint instead of leaving its own logs to be
        mistaken for earlier use. Existing state, including partial or
        damaged state, is left untouched for the commands that open it.

        Raises:
            AuthorityError: If bootstrapping finds untrustworthy earlier state.
        """
        with cls._guarded(store_dir) as (store, controls):
            if not os.path.lexists(controls / MARKER) and not os.path.lexists(controls / DATABASE):
                cls._bootstrap(store, controls, footprint)

    @staticmethod
    @contextmanager
    def _guarded(store_dir: Path) -> Iterator[tuple[Path, Path]]:
        """Hold the exclusive bootstrap guard (a `flock` on the controls directory)."""
        try:
            store = _owned_dir(store_dir.absolute())
            controls = store / CONTROLS_DIR
            controls.mkdir(mode=0o700, exist_ok=True)
            _owned_dir(controls)
            (controls / LEASES).mkdir(mode=0o700, exist_ok=True)
            _owned_dir(controls / LEASES)
            guard = os.open(controls, os.O_RDONLY)
        except OSError as exc:
            # Missing permission or a file where a directory belongs: nothing
            # is created over it or repaired.
            raise AuthorityError(
                f"control authority directory under {store_dir} cannot be used: {exc}"
            ) from exc
        try:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX)
            except OSError as exc:
                raise AuthorityError(
                    f"control authority guard on {controls} cannot be locked: {exc}"
                ) from exc
            yield store, controls
        finally:
            os.close(guard)

    @classmethod
    def _bootstrap(cls, store: Path, controls: Path, footprint: Footprint) -> None:
        now = datetime.now(tz=UTC)
        authority_id = uuid.uuid4().hex
        staging = controls / f".{DATABASE}.{authority_id}.tmp"
        try:
            cls._write_new_ledger(store, staging, authority_id, footprint, now)
            os.replace(staging, controls / DATABASE)
            marker_tmp = controls / f".{MARKER}.{authority_id}.tmp"
            marker_tmp.write_text(authority_id + "\n", encoding="utf-8")
            os.replace(marker_tmp, controls / MARKER)
        except (OSError, sqlite3.DatabaseError) as exc:
            raise AuthorityError(
                f"creating the control authority in {controls} failed: {exc}"
            ) from exc

    @staticmethod
    def _write_new_ledger(
        store: Path, staging: Path, authority_id: str, footprint: Footprint, now: datetime
    ) -> None:
        conn = sqlite3.connect(staging, isolation_level=None)
        try:
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            meta = {
                "schema_version": str(SCHEMA_VERSION),
                "authority_id": authority_id,
                "created_at": now.isoformat(),
                "last_clock": now.isoformat(),
                "continuity": "continuous",
                "footprint": json.dumps(list(footprint.existing)),
            }
            conn.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
            if not footprint.empty:
                conn.execute(
                    "INSERT INTO unknown_months VALUES (?, ?, ?)",
                    (
                        utc_month(now),
                        "earlier apprentice state existed before this authority: "
                        + ", ".join(footprint.existing),
                        now.isoformat(),
                    ),
                )
                for exposure in scan_records(store, now):
                    conn.execute(
                        "INSERT INTO known_records VALUES (?, 'legacy', ?, ?, NULL, NULL)",
                        (exposure.run_id, int(exposure.live), now.isoformat()),
                    )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[tuple[sqlite3.Connection, datetime]]:
        """Run one serialized ledger transaction with a checked UTC clock."""
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            raise AuthorityError(f"control ledger is unavailable: {exc}") from exc
        try:
            now = datetime.now(tz=UTC)
            last = _meta_instant(conn, "last_clock").isoformat()
            if now < datetime.fromisoformat(last):
                raise AuthorityError(
                    f"the UTC clock ({now.isoformat()}) is earlier than the ledger's last "
                    f"decision ({last}); holds are never expired by a clock that runs backwards"
                )
            conn.execute("UPDATE meta SET value = ? WHERE key = 'last_clock'", (now.isoformat(),))
            yield conn, now
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise AuthorityError(f"control ledger failed during a decision: {exc}") from exc
        except BaseException:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    # -- leases and recovery -------------------------------------------------

    def _lease_fd(self, slot: int) -> int:
        try:
            return _open_single_link_file(
                _owned_dir(self.controls / LEASES) / f"{slot}.lock", os.O_RDWR | os.O_CREAT, 0o600
            )
        except ArtifactError as exc:
            raise AuthorityError(str(exc)) from exc

    def recover(self, conn: sqlite3.Connection, now: datetime) -> list[str]:
        """Settle every active cycle whose lease holder has died; return their IDs."""
        lost: list[str] = []
        rows = conn.execute(
            "SELECT cycle_id, slot FROM cycles WHERE state = ?", (ACTIVE,)
        ).fetchall()
        for cycle_id, slot in rows:
            fd = self._lease_fd(slot)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                self.terminate(conn, now, cycle_id, OWNER_LOST, "the cycle's process ended")
                lost.append(cycle_id)
            finally:
                os.close(fd)
        return lost

    def terminate(
        self, conn: sqlite3.Connection, now: datetime, cycle_id: str, outcome: str, detail: str
    ) -> None:
        """Commit a cycle's terminal state: unsettled dispatches stay held as unknown."""
        conn.execute(
            "UPDATE entries SET state = ?, updated_at = ? WHERE cycle_id = ? AND state = ?",
            (UNKNOWN, now.isoformat(), cycle_id, DISPATCHED),
        )
        conn.execute(
            "UPDATE entries SET state = ?, charged_tokens = 0, charged_nanodollars = 0, "
            "updated_at = ? WHERE cycle_id = ? AND state = ?",
            (RELEASED, now.isoformat(), cycle_id, RESERVED),
        )
        conn.execute(
            "UPDATE cycles SET state = ?, outcome = ?, detail = ?, finished_at = ? "
            "WHERE cycle_id = ? AND state = ?",
            (TERMINAL, outcome, detail, now.isoformat(), cycle_id, ACTIVE),
        )

    def _acquire_slot(self, conn: sqlite3.Connection) -> tuple[int, int]:
        used = {
            row[0] for row in conn.execute("SELECT slot FROM cycles WHERE state = ?", (ACTIVE,))
        }
        slot = 0
        while True:
            if slot not in used:
                fd = self._lease_fd(slot)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                else:
                    return slot, fd
            slot += 1

    # -- exposure checks -----------------------------------------------------

    def rescan(self, conn: sqlite3.Connection, now: datetime) -> None:
        """Register run records no cycle created; their unmetered work holds the month."""
        known = {row[0] for row in conn.execute("SELECT run_id FROM known_records")}
        known |= {
            row[0] for row in conn.execute("SELECT run_id FROM cycles WHERE run_id IS NOT NULL")
        }
        for exposure in scan_records(self.store_dir, now):
            if exposure.run_id in known:
                continue
            conn.execute(
                "INSERT INTO known_records VALUES (?, 'unreferenced', ?, ?, NULL, NULL)",
                (exposure.run_id, int(exposure.live), now.isoformat()),
            )
            reason = f"run record {exposure.run_id} was written without this authority"
            for month in {exposure.started_month, utc_month(now)}:
                conn.execute(
                    "INSERT OR IGNORE INTO unknown_months VALUES (?, ?, ?)",
                    (month, reason, now.isoformat()),
                )

    def require_admissible(self, conn: sqlite3.Connection) -> None:
        continuity = _meta_value(conn, "continuity")
        if continuity != "continuous":
            raise ControlDeniedError(
                "controls.continuity",
                f"the authority is {continuity}; no work is admitted until continuity is "
                "re-established with `apprentice controls adopt-legacy`",
            )
        live = conn.execute("SELECT run_id FROM known_records WHERE live = 1").fetchall()
        if live:
            raise ControlDeniedError(
                "controls.legacy",
                f"{len(live)} run record(s) written without this authority may still be "
                f"in progress ({', '.join(r[0] for r in live[:5])}); once no earlier apprentice "
                "process is running, record that with `apprentice controls adopt-legacy`",
            )

    @staticmethod
    def _policy_conflict(conn: sqlite3.Connection, policy: ControlPolicy) -> int:
        """Live cycles that run under an effective policy other than `policy` (0 if none)."""
        stored = _stored_policy(conn)
        if stored is None or stored.to_json() == policy.to_json():
            return 0
        (live,) = conn.execute("SELECT COUNT(*) FROM cycles WHERE state = ?", (ACTIVE,)).fetchone()
        return int(live)

    def adopt_policy(self, conn: sqlite3.Connection, policy: ControlPolicy) -> None:
        """Make `policy` effective at a quiescent boundary; refuse while a cycle is live."""
        conflicting = self._policy_conflict(conn, policy)
        if conflicting:
            raise ControlDeniedError(
                "controls.policy",
                f"{conflicting} live cycle(s) run under a different effective policy; the loaded "
                "configuration is adopted once they finish",
            )
        stored = _stored_policy(conn)
        if stored is None or stored.to_json() != policy.to_json():
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('policy', ?)", (policy.to_json(),))

    # -- cycles --------------------------------------------------------------

    def begin_cycle(self, kind: str, policy: ControlPolicy, *, run_id: str | None = None) -> Cycle:
        """Admit one controlled work cycle and take its lease.

        Raises:
            ControlDeniedError: If a control refuses the cycle (nothing is held).
            AuthorityError: If the authority is not in a trustworthy state.
        """
        self.refresh()
        fd: int | None = None
        try:
            with self.transaction() as (conn, now):
                self.recover(conn, now)
                self.require_admissible(conn)
                self.adopt_policy(conn, policy)
                slot, fd = self._acquire_slot(conn)
                cycle_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO cycles VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                    (cycle_id, kind, run_id, slot, now.isoformat(), policy.to_json(), ACTIVE),
                )
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        return Cycle(self, cycle_id, kind, policy, run_id, fd)

    def refresh(self) -> None:
        """Commit owner-loss recovery and newly found unreferenced records.

        Runs in its own transaction so a later denial cannot roll back what
        it registered.
        """
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            self.rescan(conn, now)

    def adopt_legacy(self, operator: str) -> dict[str, Any]:
        """Record the operator's declaration that no earlier apprentice process is running.

        Marks every live legacy or unreferenced record as adopted. Debits,
        unknown months and holds are never cleared.

        Raises:
            ControlDeniedError: If a cycle of this authority is still live.
        """
        self.refresh()
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            live = conn.execute("SELECT COUNT(*) FROM cycles WHERE state = ?", (ACTIVE,)).fetchone()
            if live[0]:
                raise ControlDeniedError("controls.adopt-legacy", f"{live[0]} cycle(s) are live")
            adopted = [
                row[0] for row in conn.execute("SELECT run_id FROM known_records WHERE live = 1")
            ]
            conn.execute(
                "UPDATE known_records SET live = 0, adopted_at = ?, adopted_by = ? WHERE live = 1",
                (now.isoformat(), operator),
            )
        return {"adopted_records": adopted, "adopted_by": operator, "adopted_at": now.isoformat()}

    def status(self) -> dict[str, Any]:
        """Report ledger-derived state; configuration values are reported separately."""
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            self.rescan(conn, now)
            return self.describe(conn, now)

    def readiness(self, profile_sha256: str | None, policy: ControlPolicy) -> dict[str, Any]:
        """Report what the ledger itself refuses before any call of a route is sent.

        `profile_sha256` is the configured profile's digest, or None when no
        valid profile is configured; `policy` is the loaded configuration's
        policy. Blockers are continuity suspension, live legacy records, an
        unknown current month, a quarantined profile and live cycles running
        under a different effective policy (the admission's `controls.policy`
        refusal, checked without adopting anything); budgets and limits still
        decide each individual call.
        """
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            self.rescan(conn, now)
            blocked: list[dict[str, str]] = []
            continuity = _meta_value(conn, "continuity")
            if continuity != "continuous":
                blocked.append({"control": "controls.continuity", "reason": continuity})
            live = [r[0] for r in conn.execute("SELECT run_id FROM known_records WHERE live = 1")]
            if live:
                blocked.append({"control": "controls.legacy", "reason": ", ".join(live)})
            unknown = conn.execute(
                "SELECT reason FROM unknown_months WHERE month = ?", (utc_month(now),)
            ).fetchone()
            if unknown:
                blocked.append({"control": "budget.global", "reason": unknown[0]})
            if profile_sha256 is not None:
                quarantined = conn.execute(
                    "SELECT reason FROM quarantine WHERE profile_sha256 = ?", (profile_sha256,)
                ).fetchone()
                if quarantined:
                    blocked.append(
                        {"control": "provider.accounting_profile_path", "reason": quarantined[0]}
                    )
            conflicting = self._policy_conflict(conn, policy)
            if conflicting:
                blocked.append(
                    {
                        "control": "controls.policy",
                        "reason": f"{conflicting} live cycle(s) run under a different effective policy",
                    }
                )
            return {"month": utc_month(now), "blocked_by": blocked}

    def usage(self, run_ids: set[str] | None = None) -> LedgerUsage:
        """Every admitted cycle and every ledger entry, read after owner-loss recovery.

        The authoritative source of metered usage for reports: it covers every
        controlled cycle, including suggest and library cycles without a run
        record and admitted cycles that ended without any entry (for example
        denied before a call or refused by a quarantine). `run_ids`
        restricts both to the cycles of those runs. Recovery settles dead
        owners' cycles first (their dispatched bounds become unknown holds);
        nothing is admitted.
        """
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            cycle_rows = conn.execute(
                "SELECT cycle_id, kind, run_id, state, outcome FROM cycles ORDER BY admitted_at"
            ).fetchall()
            entry_rows = conn.execute(
                "SELECT e.cycle_id, c.kind, c.run_id, e.month, e.stage, e.role, e.operation, "
                "e.basis, e.state, e.bound_tokens, e.bound_nanodollars, e.charged_tokens, "
                "e.charged_nanodollars FROM entries e JOIN cycles c ON c.cycle_id = e.cycle_id "
                "ORDER BY e.created_at"
            ).fetchall()
        cycles = [
            dict(zip(("cycle_id", "kind", "run_id", "state", "outcome"), row, strict=True))
            for row in cycle_rows
        ]
        keys = (
            "cycle_id",
            "kind",
            "run_id",
            "month",
            "stage",
            "role",
            "operation",
            "basis",
            "state",
            "bound_tokens",
            "bound_nanodollars",
            "charged_tokens",
            "charged_nanodollars",
        )
        entries = [dict(zip(keys, row, strict=True)) for row in entry_rows]
        if run_ids is not None:
            cycles = [cycle for cycle in cycles if cycle["run_id"] in run_ids]
            entries = [entry for entry in entries if entry["run_id"] in run_ids]
        return LedgerUsage(cycles=cycles, entries=entries)

    def describe(self, conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        month = utc_month(now)
        policy = _stored_policy(conn)
        totals = conn.execute(
            "SELECT basis, state, COUNT(*), SUM(bound_tokens), SUM(bound_nanodollars), "
            "SUM(charged_tokens), SUM(charged_nanodollars) FROM entries WHERE month = ? "
            "GROUP BY basis, state",
            (month,),
        ).fetchall()
        return {
            "authority_id": self.authority_id,
            "controls_dir": str(self.controls),
            "continuity": _meta_value(conn, "continuity"),
            "effective_policy_sha256": policy.fingerprint() if policy is not None else None,
            "month": month,
            "month_entries": [
                {
                    "basis": basis,
                    "state": state,
                    "entries": count,
                    "bound_tokens": bound_tokens or 0,
                    "bound_nanodollars": bound_nanos or 0,
                    "charged_tokens": charged_tokens or 0,
                    "charged_nanodollars": charged_nanos or 0,
                }
                for basis, state, count, bound_tokens, bound_nanos, charged_tokens, charged_nanos in totals
            ],
            "unknown_months": [
                {"month": m, "reason": r}
                for m, r in conn.execute("SELECT month, reason FROM unknown_months ORDER BY month")
            ],
            "quarantined_profiles": [
                {"profile_sha256": p, "reason": r}
                for p, r in conn.execute("SELECT profile_sha256, reason FROM quarantine")
            ],
            "live_cycles": [
                {"cycle_id": c, "kind": k, "slot": s, "admitted_at": a}
                for c, k, s, a in conn.execute(
                    "SELECT cycle_id, kind, slot, admitted_at FROM cycles WHERE state = ?",
                    (ACTIVE,),
                )
            ],
            "legacy_records_awaiting_adoption": [
                r[0] for r in conn.execute("SELECT run_id FROM known_records WHERE live = 1")
            ],
        }


class Cycle:
    """One admitted controlled work cycle holding its lease until finished."""

    def __init__(
        self,
        authority: Authority,
        cycle_id: str,
        kind: str,
        policy: ControlPolicy,
        run_id: str | None,
        lease_fd: int,
    ) -> None:
        self.authority = authority
        self.cycle_id = cycle_id
        self.kind = kind
        self.policy = policy
        self.run_id = run_id
        self._lease_fd: int | None = lease_fd

    @property
    def finished(self) -> bool:
        return self._lease_fd is None

    def require_live(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT state FROM cycles WHERE cycle_id = ?", (self.cycle_id,)
        ).fetchone()
        if self._lease_fd is None or row is None or row[0] != ACTIVE:
            raise AuthorityError(f"cycle {self.cycle_id} is not live")

    def finish(self, outcome: str, detail: str = "") -> None:
        """Commit the terminal outcome, then release the lease.

        If the terminal state cannot be committed the error propagates and the
        lease stays held until this process ends; recovery then keeps every
        dispatched bound as unknown exposure.
        """
        if self._lease_fd is None:
            return
        with self.authority.transaction() as (conn, now):
            self.authority.terminate(conn, now, self.cycle_id, outcome, detail)
        os.close(self._lease_fd)
        self._lease_fd = None

    def summary(self) -> dict[str, Any]:
        """Derived reference to this cycle's ledger entries for its run record."""
        conn = self.authority._conn
        try:
            rows = conn.execute(
                "SELECT stage, role, operation, basis, state, bound_tokens, bound_nanodollars, "
                "charged_tokens, charged_nanodollars FROM entries WHERE cycle_id = ? "
                "ORDER BY created_at",
                (self.cycle_id,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise AuthorityError(f"control ledger is unreadable: {exc}") from exc
        return {
            "authority_id": self.authority.authority_id,
            "cycle_id": self.cycle_id,
            "kind": self.kind,
            "entries": [
                {
                    "stage": stage,
                    "role": role,
                    "operation": operation,
                    "basis": basis,
                    "state": state,
                    "bound_tokens": bound_tokens,
                    "bound_nanodollars": bound_nanos,
                    "charged_tokens": charged_tokens,
                    "charged_nanodollars": charged_nanos,
                }
                for stage, role, operation, basis, state, bound_tokens, bound_nanos, charged_tokens, charged_nanos in rows
            ],
        }

    def __enter__(self) -> Cycle:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> None:
        if self.finished:
            return
        if exc_type is None:
            self.finish("completed")
        elif issubclass(exc_type, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            self.finish("cancelled", repr(exc))
        elif issubclass(exc_type, ControlDeniedError):
            self.finish("denied", str(exc))
        else:
            self.finish("failed", repr(exc))
