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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from apprentice.controls import limits, publication
from apprentice.controls.errors import AuthorityError, ControlDeniedError
from apprentice.controls.footprint import Footprint, scan_records
from apprentice.controls.policy import ControlPolicy
from apprentice.core.artifacts import ArtifactError, _open_single_link_file, require_owned_root

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SCHEMA_VERSION = 3
CONTROLS_DIR = "controls"
MARKER = "authority.id"
DATABASE = "accounting.sqlite3"
LEASES = "leases"
# How long one ledger decision waits for another process's transaction.
BUSY_TIMEOUT_SECONDS = 60

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


def _expected_columns(script: str) -> dict[str, list[tuple[str, str, int]]]:
    """Column (name, type, not-null) lists of every table `script` defines."""
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(script)
        tables = [
            r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            table: [(c[1], c[2], c[3]) for c in probe.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }
    finally:
        probe.close()


# Schema 1 (written by earlier versions, migrated in place) and schema 2.
_COLUMNS = {
    1: _expected_columns(_SCHEMA),
    2: _expected_columns(_SCHEMA + publication.HISTORICAL_SCHEMA_V2),
    SCHEMA_VERSION: _expected_columns(_SCHEMA + publication.SCHEMA_V3),
}


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


def _validate_ledger(conn: sqlite3.Connection, version: int) -> None:
    """Require the exact structure and readable required metadata of schema `version`.

    Schemas 2 and 3 additionally require the circuit, cooldown and window metadata.

    Raises:
        AuthorityError: On page corruption, a missing, extra or altered table
            or column, or missing/garbled required metadata. Nothing is repaired.
    """
    columns = _COLUMNS[version]
    (check,) = conn.execute("PRAGMA quick_check").fetchone()
    if check != "ok":
        raise AuthorityError(f"control ledger is damaged: integrity check reports {check!r}")
    present = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    unexpected = sorted(present - set(columns))
    if unexpected:
        raise AuthorityError(
            f"control ledger is damaged: schema {version} has no table(s) {unexpected}"
        )
    for table, expected in columns.items():
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
    if version >= 2:
        if continuity == "suspended":
            _suspension(conn)
        limits.validate_meta(conn)


def _require_undamaged(conn: sqlite3.Connection, database: Path, version: int) -> None:
    """`_validate_ledger`, naming the remedy for real damage: restore from backup."""
    try:
        _validate_ledger(conn, version)
    except AuthorityError as exc:
        raise AuthorityError(f"{exc} ({database}); restore it from backup") from exc


def _suspension(conn: sqlite3.Connection) -> dict[str, str]:
    """The first rollback suspension (operator and instant) of a suspended ledger."""
    try:
        suspended = json.loads(_meta_value(conn, "suspended"))
        record = {"by": str(suspended["by"]), "at": suspended["at"]}
        moment = datetime.fromisoformat(record["at"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthorityError(
            f"control ledger is damaged: the suspension is unreadable: {exc}"
        ) from exc
    if moment.tzinfo is None:
        raise AuthorityError("control ledger is damaged: the suspension instant has no timezone")
    return record


def utc_now() -> datetime:
    """The UTC clock every ledger decision reads (once per transaction)."""
    return datetime.now(tz=UTC)


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
                conn = sqlite3.connect(database, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
            except sqlite3.DatabaseError as exc:
                raise AuthorityError(f"control ledger {database} cannot be opened: {exc}") from exc
            try:
                cls._check_and_upgrade(conn, database, authority_id, store)
            except sqlite3.DatabaseError as exc:
                conn.close()
                raise AuthorityError(f"control ledger {database} is unreadable: {exc}") from exc
            except AuthorityError:
                conn.close()
                raise
            return cls(store, conn, authority_id)

    @classmethod
    def _check_and_upgrade(
        cls, conn: sqlite3.Connection, database: Path, authority_id: str, store: Path
    ) -> None:
        """Validate the ledger as the schema it is; migrate a valid schema-1 or -2 ledger to 3.

        Each supported version has one exact structure: 1 (the control
        authority without publication state), 2 (publication state with
        run-level `known_submissions`) and 3 (submission-identity
        `known_submissions`). An older ledger is validated as its own version
        first, migrated in one transaction that keeps every row, then
        validated as the current version.

        Raises:
            AuthorityError: On a foreign, unsupported or damaged ledger, or a
                failed migration (which leaves the ledger at its version).
        """
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        found = meta.get("schema_version")
        if found not in {str(v) for v in _COLUMNS}:
            raise AuthorityError(
                f"control ledger {database} has unsupported schema {found!r}; this version "
                f"reads 1 and 2 (migrated) and {SCHEMA_VERSION}"
            )
        if meta.get("authority_id") != authority_id:
            raise AuthorityError(f"control ledger {database} belongs to another authority")
        version = int(found)
        if version != SCHEMA_VERSION:
            _require_undamaged(conn, database, version)
            try:
                if version == 1:
                    cls._migrate_v1(conn)
                else:
                    cls._migrate_v2(conn, store)
            except sqlite3.DatabaseError as exc:
                # A valid ledger whose upgrade could not complete (read-only
                # file, a lock held by another process, an I/O fault): the
                # transaction rolled back and every row is as it was.
                raise AuthorityError(
                    f"upgrading control ledger {database} to schema {SCHEMA_VERSION} did not "
                    f"complete: {exc}; it was rolled back and is unchanged (still schema "
                    f"{version}) — nothing needs restoring; clear the cause (file permissions, "
                    "another apprentice process using the ledger) and run the command again"
                ) from exc
        _require_undamaged(conn, database, SCHEMA_VERSION)

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

    @staticmethod
    def _migrate_v1(conn: sqlite3.Connection) -> None:
        """Add the publication and circuit state of schema 2 to a schema-1 ledger.

        One transaction: every schema-1 row is kept as it is, nothing is
        credited or reset. Any failure (including the commit) rolls back
        and propagates the original error.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in publication.SCHEMA_V3.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.executemany("INSERT INTO meta VALUES (?, ?)", limits.CIRCUIT_DEFAULTS.items())
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _migrate_v2(conn: sqlite3.Connection, store: Path) -> None:
        """Turn schema-2 run-level `known_submissions` rows into schema-3 identities.

        One transaction: every other table and value is kept as it is, every
        schema-2 row is carried over (`publication.migrate_known_submissions_v2`),
        nothing is credited or reset. Any failure rolls back and propagates.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            exposures = scan_records(store, utc_now())
            conn.execute("ALTER TABLE known_submissions RENAME TO known_submissions_v2")
            conn.execute(publication.KNOWN_SUBMISSIONS_V3)
            publication.migrate_known_submissions_v2(conn, exposures)
            conn.execute("DROP TABLE known_submissions_v2")
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
            raise

    @classmethod
    def _bootstrap(cls, store: Path, controls: Path, footprint: Footprint) -> None:
        now = utc_now()
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
            conn.executescript(_SCHEMA + publication.SCHEMA_V3)
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
            conn.executemany("INSERT INTO meta VALUES (?, ?)", limits.CIRCUIT_DEFAULTS.items())
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
                exposures = scan_records(store, now)
                for exposure in exposures:
                    conn.execute(
                        "INSERT INTO known_records VALUES (?, 'legacy', ?, ?, NULL, NULL)",
                        (exposure.run_id, int(exposure.live), now.isoformat()),
                    )
                publication.register_exposures(conn, now, exposures)
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
            now = utc_now()
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
        row = conn.execute(
            "SELECT policy FROM cycles WHERE cycle_id = ? AND state = ?", (cycle_id, ACTIVE)
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE cycles SET state = ?, outcome = ?, detail = ?, finished_at = ? "
            "WHERE cycle_id = ?",
            (TERMINAL, outcome, detail, now.isoformat(), cycle_id),
        )
        try:
            policy = ControlPolicy.from_json(row[0])
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            raise AuthorityError(
                f"control ledger is damaged: the policy of cycle {cycle_id} is unreadable: {exc}"
            ) from exc
        publication.settle_on_terminal(conn, now, cycle_id, outcome)
        limits.record_outcome(conn, now, cycle_id, outcome, policy)

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
        exposures = scan_records(self.store_dir, now)
        publication.register_exposures(conn, now, exposures)
        for exposure in exposures:
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

    def begin_cycle(
        self,
        kind: str,
        policy: ControlPolicy,
        *,
        run_id: str | None = None,
        publication_claim: tuple[str, tuple[str, ...]] | None = None,
    ) -> Cycle:
        """Admit one controlled work cycle (one item) and take its lease.

        Concurrent items, cooldown and the circuit are checked in the same
        transaction that records the admission. `publication_claim`
        (manifest digest, repositories) additionally reserves one PR slot per
        repository for a submit cycle of `run_id`.

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
                limits.admit_items(conn, policy)
                limits.admit_cooldown(conn, now, policy)
                cycle_id = uuid.uuid4().hex
                limits.admit_circuit(conn, now, cycle_id)
                if publication_claim is not None:
                    limits.admit_pr_slots(conn, now, policy, len(publication_claim[1]))
                slot, fd = self._acquire_slot(conn)
                conn.execute(
                    "INSERT INTO cycles VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                    (cycle_id, kind, run_id, slot, now.isoformat(), policy.to_json(), ACTIVE),
                )
                attempt_id = None
                if publication_claim is not None:
                    if run_id is None:
                        raise AuthorityError("a publication claim needs the run it publishes")
                    attempt_id = publication.claim(
                        conn, now, cycle_id, run_id, publication_claim[0], publication_claim[1]
                    )
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        return Cycle(self, cycle_id, kind, policy, run_id, fd, attempt_id, now)

    def begin_writes(self, cycle: Cycle) -> None:
        """Final admission immediately before the first remote write of `cycle`'s attempt."""
        if cycle.publication_attempt is None:
            raise AuthorityError(f"cycle {cycle.cycle_id} claimed no publication attempt")
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            cycle.require_live(conn)
            publication.begin_writes(conn, now, cycle.cycle_id, cycle.publication_attempt)

    def require_writing(self, cycle: Cycle, step: str) -> None:
        """Before one remote step of a begun publication: the cycle must still own its attempt.

        Checks the durable cycle row, the cycle's own lease (same inode as the
        lease file) and that its attempt is still writing. The circuit is not
        re-checked: an admitted publication finishes or fails on its own.

        Raises:
            AuthorityError: If the cycle, its lease or its attempt is no longer live.
        """
        if cycle.publication_attempt is None:
            raise AuthorityError(f"cycle {cycle.cycle_id} claimed no publication attempt")
        with self.transaction() as (conn, _now):
            cycle.require_live(conn)
            (slot,) = conn.execute(
                "SELECT slot FROM cycles WHERE cycle_id = ?", (cycle.cycle_id,)
            ).fetchone()
            cycle.require_lease(self.controls / LEASES / f"{slot}.lock")
            stored = publication.attempt(conn, cycle.publication_attempt)
            if stored is None or stored["state"] != publication.WRITING:
                raise AuthorityError(
                    f"publication attempt {cycle.publication_attempt} is "
                    f"{stored['state'] if stored else 'missing'}; {step} is not started"
                )

    def finish_publication(self, attempt_id: str, state: str, detail: str) -> dict[str, Any]:
        with self.transaction() as (conn, now):
            return publication.finish(conn, now, attempt_id, state, detail)

    def publication_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        """The stored attempt after owner-loss recovery has been committed.

        A cycle whose owner died is settled first, so a claimed attempt of a
        killed process reads as ended (`NO_WRITE` without a write intent,
        its slots released once) rather than as possibly still writing. An
        attempt of a live owner is returned as it is.
        """
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            return publication.attempt(conn, attempt_id)

    def reset_circuit(self, operator: str) -> dict[str, str]:
        """Close the circuit after investigation; refused while any cycle is live."""
        self.refresh()
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            return limits.reset_circuit(conn, now, operator)

    def prepare_rollback(self, operator: str) -> dict[str, str]:
        """Suspend continuity before guarded code is removed; nothing is admitted until re-adoption.

        Debits, holds, slots, receipts and records are kept. After a later
        upgrade, `adopt_legacy` re-establishes continuity and holds the
        unmetered interval as unknown.
        """
        self.refresh()
        with self.transaction() as (conn, now):
            self.recover(conn, now)
            (live,) = conn.execute(
                "SELECT COUNT(*) FROM cycles WHERE state = ?", (ACTIVE,)
            ).fetchone()
            if live:
                raise ControlDeniedError("controls.prepare-rollback", f"{live} cycle(s) are live")
            if _meta_value(conn, "continuity") == "suspended":
                # Repeating it keeps the first suspension: the unmetered
                # interval that re-adoption holds unknown starts there.
                first = _suspension(conn)
            else:
                first = {"by": operator, "at": now.isoformat()}
                conn.execute("UPDATE meta SET value = 'suspended' WHERE key = 'continuity'")
                conn.execute("INSERT INTO meta VALUES ('suspended', ?)", (json.dumps(first),))
        return {
            "continuity": "suspended",
            "suspended_by": first["by"],
            "suspended_at": first["at"],
        }

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
            restored = self._restore_continuity(conn, now)
            if adopted or restored:
                # Earlier publication may have happened at any time up to now.
                limits.fill_windows(conn, now)
        return {
            "adopted_records": adopted,
            "continuity_restored": restored,
            "adopted_by": operator,
            "adopted_at": now.isoformat(),
        }

    @staticmethod
    def _restore_continuity(conn: sqlite3.Connection, now: datetime) -> bool:
        """End a rollback suspension: hold every month of the unmetered interval as unknown."""
        if _meta_value(conn, "continuity") == "continuous":
            return False
        since = datetime.fromisoformat(_suspension(conn)["at"])
        month = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while utc_month(month) <= utc_month(now):
            conn.execute(
                "INSERT OR IGNORE INTO unknown_months VALUES (?, ?, ?)",
                (utc_month(month), "unmetered interval after prepare-rollback", now.isoformat()),
            )
            month = (month + timedelta(days=32)).replace(day=1)
        conn.execute("UPDATE meta SET value = 'continuous' WHERE key = 'continuity'")
        conn.execute("DELETE FROM meta WHERE key = 'suspended'")
        return True

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
        unknown current month, a quarantined profile, live cycles running
        under a different effective policy (the admission's `controls.policy`
        refusal, checked without adopting anything) and what would refuse a
        new cycle under `policy` now (concurrent items, cooldown, circuit);
        budgets still decide each individual call.
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
            blocked.extend(limits.cycle_blockers(conn, now, policy))
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
        anchor = limits.cooldown_anchor(conn)
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
            "circuit": {
                "state": limits.get(conn, "circuit_state"),
                "consecutive_failures": int(limits.get(conn, "circuit_failures")),
                "consecutive_opens": int(limits.get(conn, "circuit_open_streak")),
                "open_until": limits.get(conn, "circuit_open_until") or None,
                "probe_cycle": limits.get(conn, "circuit_probe") or None,
            },
            "cooldown_anchor": anchor.isoformat() if anchor else None,
            "pr_windows": limits.window_usage(conn, now),
            "publication_attempts": [
                {"attempt_id": a, "run_id": r, "state": st, "remote_write_intent": bool(w)}
                for a, r, st, w in conn.execute(
                    "SELECT attempt_id, run_id, state, remote_write_intent "
                    "FROM publication_attempts ORDER BY claimed_at DESC LIMIT 20"
                )
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
        publication_attempt: str | None,
        admitted_at: datetime,
    ) -> None:
        self.authority = authority
        self.cycle_id = cycle_id
        self.kind = kind
        self.policy = policy
        self.run_id = run_id
        self.publication_attempt = publication_attempt
        # The ledger instant of admission (and of the publication claim):
        # every later ledger decision reads a clock at least this late.
        self.admitted_at = admitted_at
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

    def require_lease(self, path: Path) -> None:
        """The cycle still holds the lease file at `path` (same inode, not replaced)."""
        if self._lease_fd is None:
            raise AuthorityError(f"cycle {self.cycle_id} released its lease")
        try:
            current = os.stat(path)
        except OSError as exc:
            raise AuthorityError(f"lease of cycle {self.cycle_id} is gone: {exc}") from exc
        held = os.fstat(self._lease_fd)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise AuthorityError(f"lease file of cycle {self.cycle_id} was replaced")

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
