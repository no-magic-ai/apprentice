"""Tests for quality gates."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from apprentice.gates.consistency import ConsistencyGate
from apprentice.gates.correctness import CorrectnessGate
from apprentice.gates.lint import LintGate
from apprentice.gates.review import ReviewGate, compute_artifact_hashes
from apprentice.gates.schema_compliance import SchemaComplianceGate
from apprentice.models.artifact import ArtifactBundle
from apprentice.models.work_item import GateVerdict, WorkItem

if TYPE_CHECKING:
    from pathlib import Path


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


class TestReviewGate:
    def test_properties(self) -> None:
        gate = ReviewGate()
        assert gate.name == "review"
        assert gate.blocking is True

    def test_fail_when_no_approval(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text("print('ok')\n")
        bundle = _make_bundle(implementation_path=str(f))
        result = ReviewGate().evaluate(_make_item(), bundle)
        assert result.verdict == GateVerdict.FAIL
        assert "no approval" in result.diagnostics["error"]
        assert "apprentice approve" in result.diagnostics["remediation"]

    def test_pass_with_matching_hashes(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text("print('ok')\n")
        bundle = _make_bundle(implementation_path=str(f))
        approval = {
            "approved_by": "tester",
            "approved_at": "2026-04-21T00:00:00+00:00",
            "artifact_hashes": compute_artifact_hashes(bundle),
        }
        result = ReviewGate(approval=approval).evaluate(_make_item(), bundle)
        assert result.verdict == GateVerdict.PASS
        assert result.diagnostics["approved_by"] == "tester"

    def test_fail_when_artifact_changed_after_approval(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text("print('ok')\n")
        bundle = _make_bundle(implementation_path=str(f))
        original_hashes = compute_artifact_hashes(bundle)
        f.write_text("print('tampered')\n")
        approval = {
            "approved_by": "tester",
            "approved_at": "2026-04-21T00:00:00+00:00",
            "artifact_hashes": original_hashes,
        }
        result = ReviewGate(approval=approval).evaluate(_make_item(), bundle)
        assert result.verdict == GateVerdict.FAIL
        assert "diffs" in result.diagnostics

    def test_fail_on_incomplete_approval(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text("print('ok')\n")
        bundle = _make_bundle(implementation_path=str(f))
        approval = {"approved_by": "tester"}  # missing approved_at and hashes
        result = ReviewGate(approval=approval).evaluate(_make_item(), bundle)
        assert result.verdict == GateVerdict.FAIL
        assert "missing fields" in result.diagnostics["error"]
