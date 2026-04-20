"""Human review gate — blocks `submit` until an operator approves artifacts.

Design (per issue #14):
- After `build` produces artifacts, the run record carries `review_approval_required=True`.
- `apprentice approve <run_id>` writes `review_approval` onto the run record,
  including a per-artifact SHA-256 fingerprint.
- This gate runs at the start of `submit` (before packaging) and FAILs when
  either no approval exists or any artifact hash has changed since approval.

The gate is pure; it reads approval metadata from the run record via the
session-state key `review_approval` populated by the CLI before launching
the submit pipeline. Any gate failure halts the pipeline before `open_pr`
can run, so operators cannot accidentally publish unapproved artifacts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apprentice.models.work_item import GateResult, GateVerdict, WorkItem

if TYPE_CHECKING:
    from apprentice.models.artifact import ArtifactBundle


def compute_artifact_hashes(bundle: ArtifactBundle) -> dict[str, str]:
    """Return a dict of artifact_name -> sha256 hex digest for every present file."""
    fields = {
        "implementation": bundle.implementation_path,
        "instrumented": bundle.instrumented_path,
        "manim_scene": bundle.manim_scene_path,
        "anki_deck": bundle.anki_deck_path,
    }
    hashes: dict[str, str] = {}
    for name, path_str in fields.items():
        if not path_str:
            continue
        path = Path(path_str)
        if not path.exists():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes[name] = digest
    return hashes


class ReviewGate:
    """Blocks `submit` unless a recorded approval matches current artifact hashes.

    Approval payload (written by `apprentice approve`):
        {
            "approved_by": "<gh_login>",
            "approved_at": "<iso8601>",
            "run_id": "<run_id>",
            "artifact_hashes": {"implementation": "<sha256>", ...}
        }
    """

    name = "review"
    max_retries = 0
    blocking = True

    def __init__(self, approval: dict[str, Any] | None = None) -> None:
        self._approval = approval or {}

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        approval = self._approval
        if not approval:
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={
                    "error": "no approval recorded",
                    "remediation": (
                        "Run `apprentice approve <run_id>` after manually "
                        "reviewing generated artifacts, then re-run `submit`."
                    ),
                },
            )

        required_fields = ("approved_by", "approved_at", "artifact_hashes")
        missing = [field for field in required_fields if field not in approval]
        if missing:
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={
                    "error": f"approval payload is missing fields: {missing}",
                    "approval": approval,
                },
            )

        expected = approval.get("artifact_hashes", {})
        current = compute_artifact_hashes(artifacts)
        diffs = {
            name: {"approved": expected.get(name, ""), "current": current.get(name, "")}
            for name in set(expected) | set(current)
            if expected.get(name) != current.get(name)
        }

        if diffs:
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={
                    "error": "artifact hashes diverge from approval — re-approve required",
                    "diffs": diffs,
                    "approved_by": approval.get("approved_by", ""),
                    "approved_at": approval.get("approved_at", ""),
                },
            )

        return GateResult(
            gate_name=self.name,
            verdict=GateVerdict.PASS,
            diagnostics={
                "approved_by": approval.get("approved_by", ""),
                "approved_at": approval.get("approved_at", ""),
                "hashes_matched": sorted(current.keys()),
            },
        )
