"""Cycle admission limits beyond budgets: items, cooldown, the circuit and PR windows.

All functions run inside one ledger transaction (`Authority.transaction`)
and read the UTC time that transaction fixed.

- `rate_limits.max_concurrent_items`: live cycles (each holds a lease) may
  not exceed the limit; every controlled cycle is an item.
- `rate_limits.cooldown_hours`: a new cycle is admitted only once the
  latest cycle admission (or a conservative cutover anchor) is that old.
  Denied admissions never move the anchor.
- `circuit_breaker.*`: one installation automated-work circuit. Failed and
  owner-lost cycles count once; completed cycles reset the count; denied and
  cancelled cycles are neutral. At the threshold the circuit opens until the
  persisted deadline, then admits exactly one probe cycle; a probe success
  closes it, a probe failure reopens it, and the configured number of
  consecutive opens latches it until `controls reset-circuit`.
- `rate_limits.max_prs_per_day`/`max_prs_per_week`: every repository PR a
  submission intends occupies one slot in rolling 24h/168h windows from its
  claim; released slots (proven never written) stop counting.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from apprentice.controls.errors import AuthorityError, ControlDeniedError
from apprentice.core.config import HOUR, MINUTE, delay

if TYPE_CHECKING:
    import sqlite3

    from apprentice.controls.policy import ControlPolicy

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half-open"
LATCHED = "latched"

QUALIFYING = frozenset({"failed", "owner-lost"})
SUCCESS = "completed"

DAY = timedelta(hours=24)
WEEK = timedelta(hours=168)
COUNTED_SLOTS = ("reserved", "used", "legacy")

CIRCUIT_DEFAULTS = {
    "circuit_state": CLOSED,
    "circuit_failures": "0",
    "circuit_open_streak": "0",
    "circuit_open_until": "",
    "circuit_probe": "",
    "cooldown_anchor": "",
    "window_fill_at": "",
}


def get(conn: sqlite3.Connection, key: str) -> str:
    """A required circuit/window metadata value; its absence is damage, never a default."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None or not isinstance(row[0], str):
        raise AuthorityError(f"control ledger is damaged: required metadata {key!r} is missing")
    return row[0]


def _instant_or_empty(conn: sqlite3.Connection, key: str) -> datetime | None:
    value = get(conn, key)
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AuthorityError(
            f"control ledger is damaged: {key} {value!r} is not a timestamp"
        ) from exc
    if moment.tzinfo is None:
        raise AuthorityError(f"control ledger is damaged: {key} {value!r} has no timezone")
    return moment


def _deadline(conn: sqlite3.Connection) -> datetime:
    deadline = _instant_or_empty(conn, "circuit_open_until")
    if deadline is None:
        raise AuthorityError("control ledger is damaged: the open circuit has no deadline")
    return deadline


def validate_meta(conn: sqlite3.Connection) -> None:
    """Require every circuit, cooldown and window value of schema 2, typed and consistent.

    Raises:
        AuthorityError: On a missing or garbled value. Nothing is repaired.
    """
    state = get(conn, "circuit_state")
    if state not in (CLOSED, OPEN, HALF_OPEN, LATCHED):
        raise AuthorityError(f"control ledger is damaged: circuit_state {state!r} is unknown")
    for key in ("circuit_failures", "circuit_open_streak"):
        value = get(conn, key)
        if not (value.isascii() and value.isdigit()):
            raise AuthorityError(f"control ledger is damaged: {key} {value!r} is not a count")
    for key in ("cooldown_anchor", "window_fill_at"):
        _instant_or_empty(conn, key)
    open_until = _instant_or_empty(conn, "circuit_open_until")
    if state == OPEN and open_until is None:
        raise AuthorityError("control ledger is damaged: the open circuit has no deadline")
    probe = get(conn, "circuit_probe")
    if probe:
        row = conn.execute("SELECT 1 FROM cycles WHERE cycle_id = ?", (probe,)).fetchone()
        if row is None:
            raise AuthorityError(f"control ledger is damaged: probe cycle {probe!r} is unknown")
    elif state == HALF_OPEN:
        raise AuthorityError("control ledger is damaged: the half-open circuit has no probe")


def put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def admit_items(conn: sqlite3.Connection, policy: ControlPolicy) -> None:
    (live,) = conn.execute("SELECT COUNT(*) FROM cycles WHERE state = 'active'").fetchone()
    if live >= policy.max_concurrent_items:
        raise ControlDeniedError(
            "rate_limits.max_concurrent_items",
            f"{live} controlled cycle(s) are live; at most {policy.max_concurrent_items} may run",
        )


def cooldown_anchor(conn: sqlite3.Connection) -> datetime | None:
    (latest,) = conn.execute("SELECT MAX(admitted_at) FROM cycles").fetchone()
    anchors = [datetime.fromisoformat(latest)] if latest else []
    stored = _instant_or_empty(conn, "cooldown_anchor")
    if stored is not None:
        anchors.append(stored)
    return max(anchors) if anchors else None


def admit_cooldown(conn: sqlite3.Connection, now: datetime, policy: ControlPolicy) -> None:
    anchor = cooldown_anchor(conn)
    if anchor is None:
        return
    eligible = anchor + delay(policy.cooldown_hours, HOUR)
    if now < eligible:
        raise ControlDeniedError(
            "rate_limits.cooldown_hours",
            f"the last cycle was admitted at {anchor.isoformat()}; the next is admitted from "
            f"{eligible.isoformat()}",
        )


def cycle_blockers(
    conn: sqlite3.Connection, now: datetime, policy: ControlPolicy
) -> list[dict[str, str]]:
    """What would refuse a new cycle now (items, cooldown, circuit), without admitting one."""
    blocked: list[dict[str, str]] = []
    (live,) = conn.execute("SELECT COUNT(*) FROM cycles WHERE state = 'active'").fetchone()
    if live >= policy.max_concurrent_items:
        blocked.append({"control": "rate_limits.max_concurrent_items", "reason": f"{live} live"})
    anchor = cooldown_anchor(conn)
    if anchor is not None and now < anchor + delay(policy.cooldown_hours, HOUR):
        eligible = anchor + delay(policy.cooldown_hours, HOUR)
        blocked.append(
            {"control": "rate_limits.cooldown_hours", "reason": f"until {eligible.isoformat()}"}
        )
    state = get(conn, "circuit_state")
    if state == LATCHED:
        blocked.append(
            {"control": "circuit_breaker.max_open_cycles_before_manual_reset", "reason": state}
        )
    elif get(conn, "circuit_probe"):
        blocked.append(
            {"control": "circuit_breaker.half_open_probe_after_minutes", "reason": "probe running"}
        )
    elif state == OPEN and now < _deadline(conn):
        blocked.append(
            {
                "control": "circuit_breaker.failure_threshold",
                "reason": f"open until {get(conn, 'circuit_open_until')}",
            }
        )
    return blocked


def admit_circuit(conn: sqlite3.Connection, now: datetime, cycle_id: str) -> bool:
    """Refuse admission while the circuit is open; return True if this cycle is the probe."""
    state = get(conn, "circuit_state")
    if state == CLOSED:
        return False
    if state == LATCHED:
        raise ControlDeniedError(
            "circuit_breaker.max_open_cycles_before_manual_reset",
            "the circuit opened the configured number of consecutive times; run "
            "`apprentice controls reset-circuit` after investigating the failures",
        )
    probe = get(conn, "circuit_probe")
    if probe:
        raise ControlDeniedError(
            "circuit_breaker.half_open_probe_after_minutes",
            f"probe cycle {probe} is running; no other cycle is admitted until it ends",
        )
    deadline = _deadline(conn)
    if now < deadline:
        raise ControlDeniedError(
            "circuit_breaker.failure_threshold",
            f"the circuit is open until {deadline.isoformat()}",
        )
    put(conn, "circuit_state", HALF_OPEN)
    put(conn, "circuit_probe", cycle_id)
    return True


def _open(conn: sqlite3.Connection, now: datetime, policy: ControlPolicy, streak: int) -> None:
    put(conn, "circuit_open_streak", str(streak))
    put(conn, "circuit_probe", "")
    if streak >= policy.max_open_cycles_before_manual_reset:
        put(conn, "circuit_state", LATCHED)
        put(conn, "circuit_open_until", "")
        return
    put(conn, "circuit_state", OPEN)
    put(
        conn,
        "circuit_open_until",
        (now + delay(policy.half_open_probe_after_minutes, MINUTE)).isoformat(),
    )


def record_outcome(
    conn: sqlite3.Connection, now: datetime, cycle_id: str, outcome: str, policy: ControlPolicy
) -> None:
    """Apply one cycle's terminal outcome to the circuit (called exactly once per cycle)."""
    state = get(conn, "circuit_state")
    if get(conn, "circuit_probe") == cycle_id:
        if outcome == SUCCESS:
            put(conn, "circuit_state", CLOSED)
            put(conn, "circuit_failures", "0")
            put(conn, "circuit_open_streak", "0")
            put(conn, "circuit_open_until", "")
            put(conn, "circuit_probe", "")
        elif outcome in QUALIFYING:
            _open(conn, now, policy, int(get(conn, "circuit_open_streak")) + 1)
        else:
            put(conn, "circuit_state", OPEN)
            put(conn, "circuit_probe", "")
        return
    if state != CLOSED:
        return
    if outcome == SUCCESS:
        put(conn, "circuit_failures", "0")
    elif outcome in QUALIFYING:
        failures = int(get(conn, "circuit_failures")) + 1
        put(conn, "circuit_failures", str(failures))
        if failures >= policy.failure_threshold:
            put(conn, "circuit_failures", "0")
            _open(conn, now, policy, 1)


def reset_circuit(conn: sqlite3.Connection, now: datetime, operator: str) -> dict[str, str]:
    """Close the circuit; budgets, holds, slots, quarantine and approvals are untouched."""
    (live,) = conn.execute("SELECT COUNT(*) FROM cycles WHERE state = 'active'").fetchone()
    if live:
        raise ControlDeniedError("controls.reset-circuit", f"{live} cycle(s) are live")
    previous = get(conn, "circuit_state")
    for key in ("circuit_state", "circuit_failures", "circuit_open_streak", "circuit_open_until"):
        put(conn, key, CIRCUIT_DEFAULTS[key])
    put(conn, "circuit_probe", "")
    put(
        conn, "circuit_reset", json.dumps({"by": operator, "at": now.isoformat(), "from": previous})
    )
    return {"previous_state": previous, "state": CLOSED, "reset_by": operator}


def fill_windows(conn: sqlite3.Connection, now: datetime) -> None:
    """Hold every PR slot and the cooldown from `now`: publication of unknown timing."""
    put(conn, "window_fill_at", now.isoformat())
    anchor_at = _instant_or_empty(conn, "cooldown_anchor")
    if anchor_at is None or anchor_at < now:
        put(conn, "cooldown_anchor", now.isoformat())


def window_usage(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    marks = ",".join("?" * len(COUNTED_SLOTS))
    counts = {}
    for name, span in (("day", DAY), ("week", WEEK)):
        (count,) = conn.execute(
            f"SELECT COUNT(*) FROM pr_slots WHERE state IN ({marks}) AND at > ?",
            (*COUNTED_SLOTS, (now - span).isoformat()),
        ).fetchone()
        counts[name] = count
    filled = _instant_or_empty(conn, "window_fill_at")
    return {
        "day": counts["day"],
        "week": counts["week"],
        "day_filled": filled is not None and now < filled + DAY,
        "week_filled": filled is not None and now < filled + WEEK,
    }


def admit_pr_slots(
    conn: sqlite3.Connection, now: datetime, policy: ControlPolicy, wanted: int
) -> None:
    usage = window_usage(conn, now)
    for name, limit in (("day", policy.max_prs_per_day), ("week", policy.max_prs_per_week)):
        control = f"rate_limits.max_prs_per_{name}"
        if usage[f"{name}_filled"]:
            raise ControlDeniedError(
                control, f"publication of unknown timing holds the whole {name} window"
            )
        if usage[name] + wanted > limit:
            raise ControlDeniedError(
                control,
                f"{wanted} PR(s) would exceed {limit} per rolling {name}: {usage[name]} slot(s) "
                "are held",
            )
