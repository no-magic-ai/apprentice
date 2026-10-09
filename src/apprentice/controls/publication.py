"""Publication attempts: PR slots, the remote-write intent and zero-effect proof.

A submit cycle claims one publication attempt with one PR slot per intended
repository before the run record's pending claim is saved. Nothing is
written remotely until `begin_writes` has persisted the attempt's remote
write intent in the same transaction that re-checks the cycle's lease and
the circuit. An attempt that ended without that intent is proof that it
wrote nothing: its slots are released and the same approved manifest may be
submitted again. Once the intent is recorded, the slots stay held and the
attempt is never retried.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from apprentice.controls.errors import AuthorityError, ControlDeniedError
from apprentice.controls.limits import fill_windows, get

if TYPE_CHECKING:
    import sqlite3

    from apprentice.controls.footprint import RecordExposure, SubmissionExposure

CLAIMED = "claimed"
WRITING = "writing"
COMPLETE = "complete"
WRITE_FAILED = "failed-after-write"
NO_WRITE = "ended-before-any-remote-write"

_PUBLICATION_TABLES = """
CREATE TABLE publication_attempts (
    attempt_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES cycles (cycle_id),
    run_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    repositories TEXT NOT NULL,
    state TEXT NOT NULL,
    remote_write_intent INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    write_intent_at TEXT,
    finished_at TEXT,
    detail TEXT
);
CREATE TABLE pr_slots (
    slot_id TEXT PRIMARY KEY,
    attempt_id TEXT,
    run_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    at TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE INDEX pr_slots_at ON pr_slots (at);
"""
# Schema 2 as the first version of these controls defined it: one row per run,
# written when a run was claimed or a foreign submission of it was registered.
HISTORICAL_SCHEMA_V2 = _PUBLICATION_TABLES + (
    "CREATE TABLE known_submissions (run_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL);\n"
)
# Schema 3 (current): one row per registered submission identity.
KNOWN_SUBMISSIONS_V3 = """CREATE TABLE known_submissions (
    identity TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
"""
SCHEMA_V3 = _PUBLICATION_TABLES + KNOWN_SUBMISSIONS_V3


def claim(
    conn: sqlite3.Connection,
    now: datetime,
    cycle_id: str,
    run_id: str,
    manifest_sha256: str,
    repositories: tuple[str, ...],
) -> str:
    attempt_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO publication_attempts VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL)",
        (
            attempt_id,
            cycle_id,
            run_id,
            manifest_sha256,
            json.dumps(list(repositories)),
            CLAIMED,
            now.isoformat(),
        ),
    )
    for repository in repositories:
        conn.execute(
            "INSERT INTO pr_slots VALUES (?, ?, ?, ?, ?, 'reserved')",
            (uuid.uuid4().hex, attempt_id, run_id, repository, now.isoformat()),
        )
    return attempt_id


def attempt(conn: sqlite3.Connection, attempt_id: str) -> dict[str, Any] | None:
    conn_row = conn.execute(
        "SELECT attempt_id, cycle_id, run_id, manifest_sha256, repositories, state, "
        "remote_write_intent, claimed_at, write_intent_at, finished_at, detail "
        "FROM publication_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if conn_row is None:
        return None
    keys = (
        "attempt_id",
        "cycle_id",
        "run_id",
        "manifest_sha256",
        "repositories",
        "state",
        "remote_write_intent",
        "claimed_at",
        "write_intent_at",
        "finished_at",
        "detail",
    )
    row: dict[str, Any] = dict(zip(keys, conn_row, strict=True))
    row["repositories"] = json.loads(row["repositories"])
    row["remote_write_intent"] = bool(row["remote_write_intent"])
    return row


def begin_writes(conn: sqlite3.Connection, now: datetime, cycle_id: str, attempt_id: str) -> None:
    """Final admission before the first remote write; persists the write intent.

    Raises:
        ControlDeniedError: If the circuit is open (or another cycle is its
            probe); the attempt has still written nothing.
        AuthorityError: If the attempt is not this live cycle's claimed attempt.
    """
    row = attempt(conn, attempt_id)
    if row is None or row["cycle_id"] != cycle_id or row["state"] != CLAIMED:
        raise AuthorityError(f"publication attempt {attempt_id} is not claimed by cycle {cycle_id}")
    state = get(conn, "circuit_state")
    if state != "closed" and get(conn, "circuit_probe") != cycle_id:
        raise ControlDeniedError(
            "circuit_breaker.failure_threshold",
            f"the circuit is {state}; nothing was written",
        )
    conn.execute(
        "UPDATE publication_attempts SET state = ?, remote_write_intent = 1, write_intent_at = ? "
        "WHERE attempt_id = ?",
        (WRITING, now.isoformat(), attempt_id),
    )
    conn.execute("UPDATE pr_slots SET state = 'used' WHERE attempt_id = ?", (attempt_id,))


def finish(
    conn: sqlite3.Connection, now: datetime, attempt_id: str, state: str, detail: str
) -> dict[str, Any]:
    """Record the attempt's end; without a write intent its slots are released."""
    row = attempt(conn, attempt_id)
    if row is None:
        raise AuthorityError(f"publication attempt {attempt_id} is missing")
    if row["remote_write_intent"] and state == NO_WRITE:
        raise AuthorityError(f"publication attempt {attempt_id} recorded a remote write intent")
    if not row["remote_write_intent"] and state != NO_WRITE:
        raise AuthorityError(f"publication attempt {attempt_id} never recorded a write intent")
    conn.execute(
        "UPDATE publication_attempts SET state = ?, finished_at = ?, detail = ? WHERE attempt_id = ?",
        (state, now.isoformat(), detail, attempt_id),
    )
    if state == NO_WRITE:
        conn.execute("UPDATE pr_slots SET state = 'released' WHERE attempt_id = ?", (attempt_id,))
    finished = attempt(conn, attempt_id)
    assert finished is not None
    return finished


def settle_on_terminal(
    conn: sqlite3.Connection, now: datetime, cycle_id: str, outcome: str
) -> None:
    """Close the cycle's unfinished publication attempt when the cycle ends.

    Called once, in the transaction that commits the cycle's terminal state
    (including owner-loss recovery). An attempt that never recorded a
    remote-write intent provably wrote nothing: it ends `NO_WRITE` and its
    slots are released. One that began writing ends `WRITE_FAILED` with its
    effects unknown and keeps its slots.
    """
    for attempt_id, state in conn.execute(
        "SELECT attempt_id, state FROM publication_attempts WHERE cycle_id = ? AND state IN (?, ?)",
        (cycle_id, CLAIMED, WRITING),
    ).fetchall():
        if state == CLAIMED:
            finish(
                conn, now, attempt_id, NO_WRITE, f"cycle ended ({outcome}) before any remote write"
            )
        else:
            finish(
                conn,
                now,
                attempt_id,
                WRITE_FAILED,
                f"cycle ended ({outcome}) after writing began; remote effects unknown",
            )


def attempt_runs(conn: sqlite3.Connection) -> dict[str, str]:
    """Attempt IDs this ledger holds proof for, each bound to the run it belongs to.

    These are its own attempts (`publication_attempts`) and the foreign
    attempts already registered under an `attempt:` identity.
    """
    bound = {
        identity.removeprefix("attempt:"): run_id
        for identity, run_id in conn.execute("SELECT identity, run_id FROM known_submissions")
        if identity.startswith("attempt:")
    }
    bound.update(conn.execute("SELECT attempt_id, run_id FROM publication_attempts").fetchall())
    return bound


def submission_identity(run_id: str, submission: SubmissionExposure, bound: dict[str, str]) -> str:
    """The stored identity of one submission attempt of `run_id`.

    An attempt claimed by an authority carries its attempt ID; one written
    without the ledger is identified by its run and its own start time, so a
    later, different submission of the same run is a different identity. An
    attempt ID that `bound` (see `attempt_runs`) ties to another run proves
    nothing about this run's submission (a copied ID), so that submission is
    identified by its run and start time too.
    """
    attempt = submission.attempt_id
    if attempt is not None and bound.get(attempt, run_id) == run_id:
        return f"attempt:{attempt}"
    return f"run:{run_id}@{submission.started.isoformat()}"


def migrate_known_submissions_v2(conn: sqlite3.Connection, exposures: list[RecordExposure]) -> None:
    """Carry schema-2 run-level rows (in `known_submissions_v2`) over to schema-3 identities.

    Runs inside the upgrade transaction. A schema-2 row of a run this ledger
    never claimed was written when a foreign submission of that run was
    registered (its slots or window hold are already booked). If the run's
    stored submission started no later than that registration, it is the
    registered one and keeps its booking under its own identity. Every other
    row — a claim marker of a run this ledger claimed, or a registration whose
    submission is gone or was replaced later — is kept as a `schema2:<run>`
    history marker, which never matches a submission: a later foreign
    submission is still registered (once) by the next admission.
    """
    owned = {row[0] for row in conn.execute("SELECT DISTINCT run_id FROM publication_attempts")}
    bound = attempt_runs(conn)
    stored = {e.run_id: e.submission for e in exposures if e.submission is not None}
    for run_id, seen_at in conn.execute(
        "SELECT run_id, seen_at FROM known_submissions_v2 ORDER BY run_id"
    ).fetchall():
        submission = stored.get(run_id)
        registered = (
            run_id not in owned
            and submission is not None
            and submission.started <= datetime.fromisoformat(seen_at)
        )
        identity = (
            submission_identity(run_id, submission, bound)
            if registered and submission
            else f"schema2:{run_id}"
        )
        if registered and submission and submission.attempt_id is not None:
            bound.setdefault(submission.attempt_id, run_id)
        conn.execute("INSERT INTO known_submissions VALUES (?, ?, ?)", (identity, run_id, seen_at))


def register_exposures(
    conn: sqlite3.Connection, now: datetime, exposures: list[RecordExposure]
) -> None:
    """Account, exactly once, for every stored submission this authority did not claim.

    Runs every admission. A submission whose attempt this ledger owns is
    already accounted for by its attempt (slots used or released). Any other
    stored submission — of an unclaimed run, or a later one of a run this
    authority once claimed — is registered under its own identity: a settled
    one with known timing occupies one slot per repository it pushed (or may
    have pushed) at its real claim time; an unsettled one holds every window
    and the cooldown from now.
    """
    known = {row[0] for row in conn.execute("SELECT identity FROM known_submissions")}
    owned = dict(conn.execute("SELECT attempt_id, run_id FROM publication_attempts").fetchall())
    bound = attempt_runs(conn)
    for exposure in exposures:
        submission = exposure.submission
        # Only an attempt this ledger claimed for this same run is already
        # accounted for; a record naming another run's attempt is foreign.
        if submission is None or owned.get(submission.attempt_id) == exposure.run_id:
            continue
        identity = submission_identity(exposure.run_id, submission, bound)
        if identity in known:
            continue
        known.add(identity)
        if submission.attempt_id is not None:
            bound.setdefault(submission.attempt_id, exposure.run_id)
        conn.execute(
            "INSERT INTO known_submissions VALUES (?, ?, ?)",
            (identity, exposure.run_id, now.isoformat()),
        )
        if not submission.settled:
            fill_windows(conn, now)
            continue
        for _ in range(submission.pushed):
            conn.execute(
                "INSERT INTO pr_slots VALUES (?, NULL, ?, 'unknown', ?, 'legacy')",
                (uuid.uuid4().hex, exposure.run_id, submission.started.isoformat()),
            )
