"""Human review gate — the required check at the root of `submit`.

`apprentice approve <run_id>` records an approval bound to the run identity
and the digest of the run's sealed bundle manifest. Before packaging touches
any repository, `submit` passes through this gate, which loads the sealed
bundle once, verifies every byte against its manifest, and requires the
approval, the run record, the bundle and the operator's requested algorithm
and tier to agree. Packaging then receives exactly the bytes verified here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apprentice.core.artifacts import BundleSnapshot
    from apprentice.core.session_store import RunRecord, SessionStore

_APPROVAL_FIELDS = frozenset(
    {"run_id", "algorithm", "tier", "manifest_sha256", "approved_by", "approved_at"}
)


class ApprovalError(Exception):
    """Raised when a run has no approval that authorizes submitting its bundle."""

    def __init__(self, message: str, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


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
        ApprovalError: If the run is not completed, has no well-formed
            approval, or the approval, run, bundle and requested identity disagree.
        ArtifactError: If the sealed bundle is missing or fails verification.
    """
    rebuild = f"apprentice build {record.algorithm_name} --tier {record.tier}"
    if record.status != "completed":
        raise ApprovalError(f"run {record.run_id} is {record.status}, not completed", rebuild)
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

    snapshot = store.load_bundle(record)
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
