"""Tests for quality gates."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from apprentice.core.artifacts import ArtifactError
from apprentice.gates.consistency import ConsistencyGate
from apprentice.gates.correctness import CorrectnessGate
from apprentice.gates.lint import LintGate
from apprentice.gates.review import ApprovalError, require_approved_snapshot
from apprentice.gates.schema_compliance import SchemaComplianceGate
from apprentice.models.artifact import ArtifactBundle
from apprentice.models.work_item import GateVerdict, WorkItem

if TYPE_CHECKING:
    from pathlib import Path

    from apprentice.core.session_store import RunRecord, SessionStore


def _make_bundle(**kwargs: str) -> ArtifactBundle:
    return ArtifactBundle(id="test", work_item_id="test", **kwargs)


def _make_item(name: str = "quicksort") -> WorkItem:
    return WorkItem(id="test", algorithm_name=name, tier=2)


_GOOD_CODE = textwrap.dedent('''\
    """Quicksort — divide and conquer sorting.

    Complexity:
        Time: O(n log n) average, O(n^2) worst
        Space: O(n)

    References:
        - Hoare, C.A.R. (1961)

    Args:
        arr: Input list.

    Returns:
        Sorted list.
    """

    from __future__ import annotations


    def quicksort(arr: list[int]) -> list[int]:
        """Sort using quicksort.

        Args:
            arr: Input list.

        Returns:
            Sorted list.

        Complexity:
            O(n log n) average.
        """
        if len(arr) <= 1:
            return arr
        pivot = arr[0]
        left = [x for x in arr[1:] if x <= pivot]
        right = [x for x in arr[1:] if x > pivot]
        return quicksort(left) + [pivot] + quicksort(right)


    if __name__ == "__main__":
        assert quicksort([3, 1, 2]) == [1, 2, 3]
        assert quicksort([]) == []
        assert quicksort([1]) == [1]
    ''')


class TestLintGate:
    def test_properties(self) -> None:
        gate = LintGate()
        assert gate.name == "lint"
        assert gate.max_retries == 2
        assert gate.blocking is True

    def test_pass_on_good_code(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        bundle = _make_bundle(implementation_path=str(f))
        result = gate_eval(LintGate(), bundle)
        assert result.verdict == GateVerdict.PASS

    def test_fail_on_syntax_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def foo(:\n  pass")
        bundle = _make_bundle(implementation_path=str(f))
        result = gate_eval(LintGate(), bundle)
        assert result.verdict == GateVerdict.FAIL

    def test_fail_on_missing_file(self) -> None:
        bundle = _make_bundle(implementation_path="/nonexistent.py")
        result = gate_eval(LintGate(), bundle)
        assert result.verdict == GateVerdict.FAIL

    def test_fail_on_empty_path(self) -> None:
        bundle = _make_bundle(implementation_path="")
        result = gate_eval(LintGate(), bundle)
        assert result.verdict == GateVerdict.FAIL


class TestCorrectnessGate:
    def test_properties(self) -> None:
        gate = CorrectnessGate()
        assert gate.name == "correctness"
        assert gate.max_retries == 1

    def test_pass_on_good_code(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        bundle = _make_bundle(implementation_path=str(f))
        result = gate_eval(CorrectnessGate(), bundle)
        assert result.verdict == GateVerdict.PASS

    def test_fail_on_assertion_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text('if __name__ == "__main__":\n    assert False\n')
        bundle = _make_bundle(implementation_path=str(f))
        result = gate_eval(CorrectnessGate(), bundle)
        assert result.verdict == GateVerdict.FAIL


class TestConsistencyGate:
    def test_properties(self) -> None:
        gate = ConsistencyGate()
        assert gate.name == "consistency"
        assert gate.max_retries == 0

    def test_pass_with_valid_impl(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        bundle = _make_bundle(implementation_path=str(f))
        result = gate_eval(ConsistencyGate(), bundle, name="quicksort")
        assert result.verdict in {GateVerdict.PASS, GateVerdict.WARN}


class TestSchemaComplianceGate:
    def test_properties(self) -> None:
        gate = SchemaComplianceGate()
        assert gate.name == "schema_compliance"
        assert gate.max_retries == 0

    def test_pass_with_good_implementation(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        bundle = _make_bundle(implementation_path=str(f))
        result = gate_eval(SchemaComplianceGate(), bundle)
        assert result.verdict in {GateVerdict.PASS, GateVerdict.WARN}


def gate_eval(
    gate: LintGate | CorrectnessGate | ConsistencyGate | SchemaComplianceGate,
    bundle: ArtifactBundle,
    name: str = "quicksort",
) -> object:
    item = _make_item(name)
    return gate.evaluate(item, bundle)


def _approved_record(store: SessionStore, algorithm: str = "selection", tier: int = 2) -> RunRecord:
    record = store.create_run(algorithm, tier)
    store.complete_run(
        record, {"generated_code": "impl = 1\n", "manim_scene_code": "scene = 1\n"}, {}, 1.0
    )
    snapshot = store.load_bundle(record)
    record.approval = {
        "run_id": snapshot.run_id,
        "algorithm": snapshot.algorithm,
        "tier": snapshot.tier,
        "manifest_sha256": snapshot.manifest_sha256,
        "approved_by": "tester",
        "approved_at": "2026-10-06T00:00:00+00:00",
    }
    return store.save(record)


class TestRequireApprovedSnapshot:
    def test_matching_approval_returns_verified_bytes(self, store: SessionStore) -> None:
        record = _approved_record(store)
        snapshot = require_approved_snapshot(store, record, algorithm="selection", tier=2)
        assert {a.role: a.data for a in snapshot.artifacts} == {
            "implementation": b"impl = 1\n",
            "manim_scene": b"scene = 1\n",
        }

    def test_missing_approval_fails_with_approve_remediation(self, store: SessionStore) -> None:
        record = _approved_record(store)
        record.approval = {}
        with pytest.raises(ApprovalError) as excinfo:
            require_approved_snapshot(store, record, algorithm="selection", tier=None)
        assert excinfo.value.remediation == f"apprentice approve {record.run_id}"

    def test_hash_only_approval_is_not_accepted(self, store: SessionStore) -> None:
        record = _approved_record(store)
        record.approval = {
            "approved_by": "tester",
            "approved_at": "2026-10-06T00:00:00+00:00",
            "artifact_hashes": {},
        }
        with pytest.raises(ApprovalError, match="not bound to a sealed bundle manifest"):
            require_approved_snapshot(store, record, algorithm="selection", tier=None)

    def test_wrong_algorithm_fails(self, store: SessionStore) -> None:
        record = _approved_record(store)
        with pytest.raises(ApprovalError, match="not 'quicksort'"):
            require_approved_snapshot(store, record, algorithm="quicksort", tier=None)

    def test_wrong_tier_fails(self, store: SessionStore) -> None:
        record = _approved_record(store)
        with pytest.raises(ApprovalError, match="not tier 3"):
            require_approved_snapshot(store, record, algorithm="selection", tier=3)

    def test_approval_from_another_run_fails(self, store: SessionStore) -> None:
        record = _approved_record(store)
        other = _approved_record(store)
        record.approval = dict(other.approval)
        with pytest.raises(ApprovalError, match="does not match its sealed bundle"):
            require_approved_snapshot(store, record, algorithm="selection", tier=None)

    def test_approval_of_a_different_manifest_fails(self, store: SessionStore) -> None:
        record = _approved_record(store)
        record.approval["manifest_sha256"] = "0" * 64
        with pytest.raises(ApprovalError, match="does not match its sealed bundle"):
            require_approved_snapshot(store, record, algorithm="selection", tier=None)

    def test_tampered_bundle_fails(self, store: SessionStore) -> None:
        record = _approved_record(store)
        path = store.bundle_dir(record.run_id) / "scene.py"
        path.chmod(0o644)
        path.write_bytes(b"scene = 2\n")
        with pytest.raises(ArtifactError, match="differ from its manifest"):
            require_approved_snapshot(store, record, algorithm="selection", tier=None)

    def test_incomplete_run_fails(self, store: SessionStore) -> None:
        record = store.create_run("selection", 2)
        with pytest.raises(ApprovalError, match="not completed"):
            require_approved_snapshot(store, record, algorithm="selection", tier=None)
