"""Atomic reservation and once-only settlement of metered model operations.

Every scope a call belongs to is checked in the same transaction: the UTC
admission month, the cycle, the stage, the role's percentage allowance and
the single call. Tokens are complete input plus all output (cached input and
reasoning are subsets, never added again); USD is the policy quote in whole
nanodollars.

Holds count at their full bound while reserved or dispatched. A settled
entry counts its charge, an unknown one (dispatched, then failed, cancelled
or orphaned) keeps its full bound forever, and a released one counts zero.
A request that does not fit waits only when active holds — which may still
release unused allowance — are the sole obstacle; committed spend, unknown
holds or a hard ceiling deny it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from apprentice.controls.authority import (
    DISPATCHED,
    RELEASED,
    RESERVED,
    SETTLED,
    UNKNOWN,
    utc_month,
)
from apprentice.controls.errors import AuthorityError, ControlDeniedError
from apprentice.controls.policy import IMPLEMENTATION_ROLE

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from apprentice.controls.authority import Cycle
    from apprentice.metering.pricing import ModelPrice

COUNT = "count"
GENERATE = "generate"
_WAIT_SECONDS = 0.05


@dataclass(frozen=True)
class Scope:
    """One limit a call is admitted against."""

    control: str
    where: str
    params: tuple[str, ...]
    limit: int


@dataclass(frozen=True)
class GenerationLimits:
    """What bounds one generation request besides the configured budgets.

    `context_tokens` is a non-hosted model's total window (input plus output);
    `max_input_tokens` a hosted model's input maximum. `requested_cap` is an
    output cap the caller asked for explicitly; it is never raised.
    """

    profile_sha256: str
    basis: str
    price: ModelPrice | None
    max_output_tokens: int
    context_tokens: int | None
    max_input_tokens: int | None
    requested_cap: int | None


@dataclass(frozen=True)
class Admission:
    entry_id: str
    cap: int
    bound_tokens: int
    bound_nanodollars: int
    binding: str


class Attempt:
    """One logical model attempt: an optional counter operation plus its generation.

    The UTC month is fixed when the first operation is admitted and covers
    every operation of the attempt.
    """

    def __init__(self, cycle: Cycle, stage: str, role: str) -> None:
        self.cycle = cycle
        self.stage = stage
        self.role = role
        self.attempt_id = uuid.uuid4().hex
        self.month: str | None = None

    def token_scopes(self) -> list[Scope]:
        policy = self.cycle.policy
        assert self.month is not None
        scopes = [
            Scope(
                "budget.global.monthly_token_ceiling",
                "month = ?",
                (self.month,),
                policy.monthly_token_ceiling,
            ),
            Scope(
                "budget.cycle.max_tokens_per_cycle",
                "cycle_id = ?",
                (self.cycle.cycle_id,),
                policy.max_tokens_per_cycle,
            ),
            Scope(
                "budget.stage.max_tokens_per_stage",
                "cycle_id = ? AND stage = ?",
                (self.cycle.cycle_id, self.stage),
                policy.max_tokens_per_stage,
            ),
        ]
        role_tokens = policy.role_tokens(self.role)
        if role_tokens is not None:
            scopes.append(
                Scope(
                    _role_control(self.role),
                    "cycle_id = ? AND role = ?",
                    (self.cycle.cycle_id, self.role),
                    role_tokens,
                )
            )
        return scopes

    def cost_scopes(self) -> list[Scope]:
        policy = self.cycle.policy
        assert self.month is not None
        scopes = [
            Scope(
                "budget.global.monthly_cost_ceiling_usd",
                "month = ?",
                (self.month,),
                policy.monthly_cost_ceiling_nanodollars,
            ),
            Scope(
                "budget.cycle.max_cost_per_cycle_usd",
                "cycle_id = ?",
                (self.cycle.cycle_id,),
                policy.max_cost_per_cycle_nanodollars,
            ),
        ]
        role_nanos = policy.role_nanodollars(self.role)
        if role_nanos is not None:
            scopes.append(
                Scope(
                    _role_control(self.role),
                    "cycle_id = ? AND role = ?",
                    (self.cycle.cycle_id, self.role),
                    role_nanos,
                )
            )
        return scopes


def _role_control(role: str) -> str:
    if role == IMPLEMENTATION_ROLE:
        return "budget.agent.implementation_budget_pct"
    return "budget.agent.tool_agent_budget_pct"


def _usage(conn: sqlite3.Connection, scope: Scope, column: str) -> tuple[int, int]:
    """Return (committed, active) amounts of `column` ('tokens' or 'nanodollars') in `scope`."""
    committed, active = conn.execute(
        f"SELECT "
        f"COALESCE(SUM(CASE WHEN state IN ('{RESERVED}', '{DISPATCHED}') THEN 0 "
        f"WHEN state = '{UNKNOWN}' THEN bound_{column} "
        f"ELSE COALESCE(charged_{column}, 0) END), 0), "
        f"COALESCE(SUM(CASE WHEN state IN ('{RESERVED}', '{DISPATCHED}') "
        f"THEN bound_{column} ELSE 0 END), 0) "
        f"FROM entries WHERE {scope.where}",
        scope.params,
    ).fetchone()
    return int(committed), int(active)


def _admission_checks(attempt: Attempt, conn: sqlite3.Connection, profile_sha256: str) -> None:
    attempt.cycle.require_live(conn)
    attempt.cycle.authority.require_admissible(conn)
    quarantined = conn.execute(
        "SELECT reason FROM quarantine WHERE profile_sha256 = ?", (profile_sha256,)
    ).fetchone()
    if quarantined:
        raise ControlDeniedError(
            "provider.accounting_profile_path",
            f"profile {profile_sha256} is quarantined: {quarantined[0]}",
        )
    unknown = conn.execute(
        "SELECT reason FROM unknown_months WHERE month = ?", (attempt.month,)
    ).fetchone()
    if unknown:
        raise ControlDeniedError(
            "budget.global",
            f"usage in {attempt.month} is unknown and held at the monthly ceilings: {unknown[0]}",
        )


def _insert(
    conn: sqlite3.Connection,
    now: datetime,
    attempt: Attempt,
    operation: str,
    basis: str,
    profile_sha256: str,
    bound_tokens: int,
    bound_nanodollars: int,
    receipt: dict[str, Any],
) -> str:
    entry_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
        (
            entry_id,
            attempt.cycle.cycle_id,
            attempt.attempt_id,
            attempt.month,
            attempt.stage,
            attempt.role,
            operation,
            basis,
            profile_sha256,
            RESERVED,
            bound_tokens,
            bound_nanodollars,
            json.dumps(receipt, sort_keys=True, default=str),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    return entry_id


async def reserve_counter(
    attempt: Attempt,
    *,
    profile_sha256: str,
    basis: str,
    fee_nanodollars: int,
    receipt: dict[str, Any],
) -> str:
    """Reserve the counting operation's full fee bound in every USD scope.

    Raises:
        ControlDeniedError: If the bound cannot fit beside committed spend.
    """
    authority = attempt.cycle.authority
    while True:
        with authority.transaction() as (conn, now):
            authority.recover(conn, now)
            if attempt.month is None:
                attempt.month = utc_month(now)
            _admission_checks(attempt, conn, profile_sha256)
            blocked_by_active = False
            for scope in attempt.cost_scopes():
                committed, active = _usage(conn, scope, "nanodollars")
                if committed + fee_nanodollars > scope.limit:
                    raise ControlDeniedError(
                        scope.control,
                        f"counting fee bound {fee_nanodollars} nanodollars exceeds the "
                        f"remaining {max(0, scope.limit - committed)}",
                    )
                if committed + active + fee_nanodollars > scope.limit:
                    blocked_by_active = True
            if not blocked_by_active:
                return _insert(
                    conn,
                    now,
                    attempt,
                    COUNT,
                    basis,
                    profile_sha256,
                    0,
                    fee_nanodollars,
                    {"admission": receipt},
                )
        await asyncio.sleep(_WAIT_SECONDS)


def _cost_cap(price: ModelPrice | None, input_tokens: int, available: int) -> int | None:
    """Largest output that keeps the worst-case quote within `available` nanodollars."""
    if price is None:
        return None
    rates = price.rates_for(input_tokens)
    room = Decimal(available) - input_tokens * rates.input
    if room < 0:
        return -1
    return int((room / rates.output).to_integral_value(ROUND_FLOOR))


def _cap(
    attempt: Attempt,
    conn: sqlite3.Connection,
    input_tokens: int,
    limits: GenerationLimits,
    *,
    with_active: bool,
) -> tuple[int, str]:
    """Return the largest admissible output cap and the control that binds it."""
    policy = attempt.cycle.policy
    candidates: list[tuple[int, str]] = [
        (limits.max_output_tokens, "provider.accounting_profile_path max_output_tokens"),
        (policy.max_tokens_per_agent_call - input_tokens, "budget.agent.max_tokens_per_agent_call"),
    ]
    if limits.context_tokens is not None:
        candidates.append(
            (limits.context_tokens - input_tokens, "provider.accounting_profile_path context")
        )
    if limits.requested_cap is not None:
        candidates.append((limits.requested_cap, "requested max_output_tokens"))
    for scope in attempt.token_scopes():
        committed, active = _usage(conn, scope, "tokens")
        used = committed + (active if with_active else 0)
        candidates.append((scope.limit - used - input_tokens, scope.control))
    for scope in attempt.cost_scopes():
        committed, active = _usage(conn, scope, "nanodollars")
        used = committed + (active if with_active else 0)
        cap = _cost_cap(limits.price, input_tokens, scope.limit - used)
        if cap is not None:
            candidates.append((cap, scope.control))
    return min(candidates)


async def admit_generation(
    attempt: Attempt, *, input_tokens: int, limits: GenerationLimits, receipt: dict[str, Any]
) -> Admission:
    """Reserve counted input plus the largest output every scope admits.

    The cap adapts to current headroom including other calls' active holds,
    so concurrent calls may receive different caps; it is a ceiling, not an
    entitlement, and is recorded in the receipt.

    Raises:
        ControlDeniedError: If no positive output fits beside committed spend,
            unknown holds and hard ceilings (including input above the model's
            input maximum).
    """
    if limits.max_input_tokens is not None and input_tokens > limits.max_input_tokens:
        raise ControlDeniedError(
            "provider.accounting_profile_path max_input_tokens",
            f"counted input {input_tokens} exceeds the model input maximum "
            f"{limits.max_input_tokens}",
        )
    authority = attempt.cycle.authority
    while True:
        with authority.transaction() as (conn, now):
            authority.recover(conn, now)
            if attempt.month is None:
                attempt.month = utc_month(now)
            _admission_checks(attempt, conn, limits.profile_sha256)
            cap, binding = _cap(attempt, conn, input_tokens, limits, with_active=True)
            if cap >= 1:
                bound_tokens = input_tokens + cap
                bound_nanos = (
                    limits.price.worst_case_nanodollars(input_tokens, cap) if limits.price else 0
                )
                entry_id = _insert(
                    conn,
                    now,
                    attempt,
                    GENERATE,
                    limits.basis,
                    limits.profile_sha256,
                    bound_tokens,
                    bound_nanos,
                    {
                        "admission": {
                            **receipt,
                            "input_tokens": input_tokens,
                            "max_output_tokens": cap,
                            "binding_control": binding,
                            "requested_max_output_tokens": limits.requested_cap,
                        }
                    },
                )
                return Admission(entry_id, cap, bound_tokens, bound_nanos, binding)
            idle_cap, idle_binding = _cap(attempt, conn, input_tokens, limits, with_active=False)
            if idle_cap < 1:
                raise ControlDeniedError(
                    idle_binding,
                    f"no output fits after {input_tokens} counted input tokens "
                    f"(stage {attempt.stage}, role {attempt.role})",
                )
        await asyncio.sleep(_WAIT_SECONDS)


def _update(
    attempt: Attempt,
    entry_id: str,
    expected: tuple[str, ...],
    sql: str,
    params: tuple[object, ...],
    receipt_key: str,
    receipt: dict[str, Any],
    quarantine: str | None = None,
) -> None:
    authority = attempt.cycle.authority
    with authority.transaction() as (conn, now):
        row = conn.execute(
            "SELECT state, receipt, profile_sha256 FROM entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None or row[0] not in expected:
            raise AuthorityError(
                f"ledger entry {entry_id} is {row[0] if row else 'missing'}, not {expected}"
            )
        stored = json.loads(row[1])
        stored[receipt_key] = receipt
        conn.execute(
            f"UPDATE entries SET {sql}, receipt = ?, updated_at = ? WHERE entry_id = ?",
            (*params, json.dumps(stored, sort_keys=True, default=str), now.isoformat(), entry_id),
        )
        if quarantine is not None:
            conn.execute(
                "INSERT OR IGNORE INTO quarantine VALUES (?, ?, ?, ?)",
                (row[2], quarantine, entry_id, now.isoformat()),
            )


def mark_dispatched(attempt: Attempt, entry_id: str, receipt: dict[str, Any]) -> None:
    """Persist the intent to send, before any byte reaches the transport."""
    _update(attempt, entry_id, (RESERVED,), "state = ?", (DISPATCHED,), "dispatch", receipt)


def settle(
    attempt: Attempt,
    entry_id: str,
    *,
    tokens: int,
    nanodollars: int,
    receipt: dict[str, Any],
    quarantine: str | None = None,
) -> None:
    """Settle a dispatched entry exactly once with its charge.

    A repeated identical settlement is a no-op; a conflicting one is retained
    at the larger charge and quarantines the profile. `quarantine` marks the
    route's profile unsafe (contradictory or above-bound usage).
    """
    authority = attempt.cycle.authority
    with authority.transaction() as (conn, now):
        row = conn.execute(
            "SELECT state, charged_tokens, charged_nanodollars, profile_sha256, receipt "
            "FROM entries WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise AuthorityError(f"ledger entry {entry_id} is missing")
        state, charged_tokens, charged_nanos, profile, stored_receipt = row
        stored = json.loads(stored_receipt)
        if state == SETTLED:
            if (charged_tokens, charged_nanos) == (tokens, nanodollars):
                return
            quarantine = (
                f"conflicting settlement of {entry_id}: {charged_tokens}/{charged_nanos} "
                f"then {tokens}/{nanodollars}"
            )
            tokens = max(tokens, charged_tokens)
            nanodollars = max(nanodollars, charged_nanos)
            stored["conflicting_settlement"] = receipt
        elif state != DISPATCHED:
            raise AuthorityError(f"ledger entry {entry_id} is {state}, not dispatched")
        else:
            stored["settlement"] = receipt
        conn.execute(
            "UPDATE entries SET state = ?, charged_tokens = ?, charged_nanodollars = ?, "
            "receipt = ?, updated_at = ? WHERE entry_id = ?",
            (
                SETTLED,
                tokens,
                nanodollars,
                json.dumps(stored, sort_keys=True, default=str),
                now.isoformat(),
                entry_id,
            ),
        )
        if quarantine is not None:
            conn.execute(
                "INSERT OR IGNORE INTO quarantine VALUES (?, ?, ?, ?)",
                (profile, quarantine, entry_id, now.isoformat()),
            )


def retain_unknown(
    attempt: Attempt, entry_id: str, receipt: dict[str, Any], *, quarantine: str | None = None
) -> None:
    """Keep a dispatched entry's full bound: its outcome or usage is unknown.

    `quarantine` additionally marks the route's profile unsafe.
    """
    _update(
        attempt, entry_id, (DISPATCHED,), "state = ?", (UNKNOWN,), "unknown", receipt, quarantine
    )


def release(attempt: Attempt, entry_id: str, receipt: dict[str, Any]) -> None:
    """Release an entry that was never dispatched."""
    _update(
        attempt,
        entry_id,
        (RESERVED,),
        "state = ?, charged_tokens = 0, charged_nanodollars = 0",
        (RELEASED,),
        "released",
        receipt,
    )
