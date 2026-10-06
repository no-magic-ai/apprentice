"""Human review gate — the required check at the root of `submit`.

`apprentice approve <run_id>` records an approval bound to the run identity
and the digest of the run's sealed bundle manifest. Before packaging touches
any repository, `submit` passes through this gate, which loads the sealed
bundle once, verifies every byte against its manifest, and requires the
approval, the run record, the bundle and the operator's requested algorithm
and tier to agree. A run whose build recorded a failed blocking gate is
never reviewable. Packaging then receives exactly the bytes verified here.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from apprentice.core.session_store import blocking_gate_failures, describe_gate_failures

if TYPE_CHECKING:
    from apprentice.core.artifacts import BundleSnapshot
    from apprentice.core.session_store import RunRecord, SessionStore

_APPROVAL_FIELDS = frozenset(
    {"run_id", "algorithm", "tier", "manifest_sha256", "approved_by", "approved_at"}
)


_APPROVER_FORBIDDEN = ("\r", "\n", "\0")


def approver_problem(approver: object) -> str | None:
    """Return why `approver` cannot name the human who approved a run, or None.

    An approver is a non-blank string without CR, LF or NUL; it is stored and
    written into commit trailers and PR bodies exactly as given.
    """
    if not isinstance(approver, str) or not approver.strip():
        return "approver must be a non-blank string"
    if any(char in approver for char in _APPROVER_FORBIDDEN):
        return "approver must not contain CR, LF or NUL characters"
    return None


def _approval_time_problem(approved_at: object) -> str | None:
    """Return why `approved_at` is not a canonical timezone-aware ISO timestamp, or None."""
    if not isinstance(approved_at, str) or not approved_at.strip():
        return "approved_at must be a non-blank ISO 8601 timestamp"
    try:
        parsed = datetime.fromisoformat(approved_at)
    except ValueError:
        return f"approved_at {approved_at!r} is not an ISO 8601 timestamp"
    if parsed.utcoffset() is None:
        return f"approved_at {approved_at!r} has no timezone"
    if parsed.isoformat() != approved_at:
        return f"approved_at {approved_at!r} is not in canonical ISO 8601 form"
    return None


class ApprovalError(Exception):
    """Raised when a run has no approval that authorizes submitting its bundle."""

    def __init__(self, message: str, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


def require_reviewable_snapshot(store: SessionStore, record: RunRecord) -> BundleSnapshot:
    """Return the verified sealed bundle of a run that may be approved or submitted.

    Checked in this order so each failure names the step that can fix it: the
    run must be completed; its sealed bundle must exist and verify (a run from
    before sealed bundles gets the rebuild instruction); and no blocking gate
    may have failed during its build (runs sealed before that refusal existed
    are caught here).

    Raises:
        ApprovalError: If the run is not completed or a blocking gate failed.
        ArtifactError: If the sealed bundle is missing or fails verification.
    """
    rebuild = f"apprentice build {record.algorithm_name} --tier {record.tier}"
    if record.status != "completed":
        raise ApprovalError(f"run {record.run_id} is {record.status}, not completed", rebuild)
    snapshot = store.load_bundle(record)
    failures = blocking_gate_failures(record.budget_summary)
    if failures:
        raise ApprovalError(
            f"run {record.run_id} cannot be approved or submitted: "
            f"{describe_gate_failures(failures)}",
            rebuild,
        )
    return snapshot


def require_approved_snapshot(
    store: SessionStore, record: RunRecord, *, algorithm: str, tier: int | None
) -> BundleSnapshot:
    """Return the verified sealed bundle that `record`'s approval authorizes.

    Args:
        store: Store owning the run's sealed bundle.
        record: Run record holding the approval.
        algorithm: Algorithm the operator asked to submit.
        tier: Tier the operator asked to submit, or None to accept the approved tier.

    Raises:
        ApprovalError: If the run is not reviewable (see
            `require_reviewable_snapshot`), has no well-formed approval, or the
            approval, run, bundle and requested identity disagree.
        ArtifactError: If the sealed bundle is missing or fails verification.
    """
    snapshot = require_reviewable_snapshot(store, record)
    if algorithm != record.algorithm_name:
        raise ApprovalError(
            f"run {record.run_id} built {record.algorithm_name!r}, not {algorithm!r}",
            f"apprentice submit {record.algorithm_name} --run-id {record.run_id}",
        )
    if tier is not None and tier != record.tier:
        raise ApprovalError(
            f"run {record.run_id} is tier {record.tier}, not tier {tier}",
            f"apprentice submit {record.algorithm_name} --run-id {record.run_id}",
        )
    approval = record.approval
    if not approval:
        raise ApprovalError(
            f"no human-review approval recorded for run {record.run_id}",
            f"apprentice approve {record.run_id}",
        )
    if set(approval) != _APPROVAL_FIELDS:
        raise ApprovalError(
            f"approval of run {record.run_id} is not bound to a sealed bundle manifest",
            f"apprentice approve {record.run_id}",
        )
    problem = approver_problem(approval["approved_by"]) or _approval_time_problem(
        approval["approved_at"]
    )
    if problem:
        raise ApprovalError(
            f"approval of run {record.run_id} is malformed: {problem}",
            f"review the bundle with `apprentice preview --run-id {record.run_id}` "
            f"and re-approve with `apprentice approve {record.run_id}`",
        )
    approved = (
        approval["run_id"],
        approval["algorithm"],
        approval["tier"],
        approval["manifest_sha256"],
    )
    if approved != (snapshot.run_id, snapshot.algorithm, snapshot.tier, snapshot.manifest_sha256):
        raise ApprovalError(
            f"approval of run {record.run_id} does not match its sealed bundle",
            f"review the bundle with `apprentice preview --run-id {record.run_id}` "
            f"and re-approve with `apprentice approve {record.run_id}`",
        )
    return snapshot
