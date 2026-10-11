"""Tests for quality gates."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from apprentice.gates.consistency import ConsistencyGate
from apprentice.gates.correctness import CorrectnessGate
from apprentice.gates.lint import LintGate
from apprentice.gates.review import ApprovalError, require_approved_snapshot
from apprentice.gates.scene_render import SceneRenderGate
from apprentice.gates.schema_compliance import SchemaComplianceGate
from apprentice.models.artifact import ArtifactBundle
from apprentice.models.work_item import GateVerdict, WorkItem
from apprentice.sandbox.errors import StagingError

if TYPE_CHECKING:
    from pathlib import Path

    from apprentice.core.session_store import RunRecord, SessionStore
    from tests.conftest import JudgedSandbox


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
    def test_pass_on_good_code(self, tmp_path: Path, judged_execution: JudgedSandbox) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        bundle = _make_bundle(implementation_path=str(f))
        result = gate_eval(CorrectnessGate(judged_execution, tmp_path), bundle)
        assert result.verdict == GateVerdict.PASS
        assert judged_execution.programs == [_GOOD_CODE.encode()]

    def test_fail_on_assertion_error(self, tmp_path: Path, judged_execution: JudgedSandbox) -> None:
        f = tmp_path / "bad.py"
        f.write_text(
            'if __name__ == "__main__":\n    assert False, "fixture: deliberate failure"\n'
        )
        bundle = _make_bundle(implementation_path=str(f))
        result = gate_eval(CorrectnessGate(judged_execution, tmp_path), bundle)
        assert result.verdict == GateVerdict.FAIL
        assert result.diagnostics["error"] == "the program exited with status 1"


_SCENE = "from manim import Scene, Text\n\n\nclass Shown(Scene):\n    def construct(self):\n        self.add(Text('x'))\n"


class TestSceneRenderGate:
    def test_no_scene_artifact_has_nothing_to_render(
        self, tmp_path: Path, judged_execution: JudgedSandbox
    ) -> None:
        result = gate_eval(SceneRenderGate(judged_execution, tmp_path), _make_bundle())
        assert result.verdict == GateVerdict.PASS
        assert judged_execution.scenes == []

    def test_rendered_video_is_written_as_derived_output(
        self, tmp_path: Path, judged_execution: JudgedSandbox
    ) -> None:
        (tmp_path / "scene.py").write_text(_SCENE)
        bundle = _make_bundle(manim_scene_path=str(tmp_path / "scene.py"))

        result = gate_eval(SceneRenderGate(judged_execution, tmp_path), bundle)

        assert result.verdict == GateVerdict.PASS
        assert judged_execution.scenes == ["Shown"]
        assert (tmp_path / "render" / "scene.mp4").read_bytes() == b"judged preview"

    def test_failed_render_fails_the_gate_and_writes_nothing(
        self, tmp_path: Path, judged_execution: JudgedSandbox
    ) -> None:
        (tmp_path / "scene.py").write_text(_SCENE + "# fixture: deliberate scene failure\n")
        bundle = _make_bundle(manim_scene_path=str(tmp_path / "scene.py"))

        result = gate_eval(SceneRenderGate(judged_execution, tmp_path), bundle)

        assert result.verdict == GateVerdict.FAIL
        assert result.diagnostics["error"] == "the scene render exited with status 1"
        assert not (tmp_path / "render").exists()

    @pytest.mark.parametrize(
        "source",
        ["class A:\n    pass\n", _SCENE + "\n\nclass Other(Scene):\n    pass\n", "class (:\n"],
    )
    def test_scene_without_exactly_one_scene_class_is_not_rendered(
        self, tmp_path: Path, judged_execution: JudgedSandbox, source: str
    ) -> None:
        (tmp_path / "scene.py").write_text(source)
        bundle = _make_bundle(manim_scene_path=str(tmp_path / "scene.py"))

        result = gate_eval(SceneRenderGate(judged_execution, tmp_path), bundle)

        assert result.verdict == GateVerdict.FAIL
        assert judged_execution.scenes == []

    def test_symlinked_scene_is_refused(
        self, tmp_path: Path, judged_execution: JudgedSandbox
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "scene.py").write_text(_SCENE)
        (root / "scene.py").symlink_to(tmp_path / "scene.py")
        bundle = _make_bundle(manim_scene_path=str(root / "scene.py"))

        with pytest.raises(StagingError):
            gate_eval(SceneRenderGate(judged_execution, root), bundle)
        assert judged_execution.scenes == []


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
    gate: LintGate | CorrectnessGate | ConsistencyGate | SchemaComplianceGate | SceneRenderGate,
    bundle: ArtifactBundle,
    name: str = "quicksort",
) -> Any:
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

    def test_hash_only_approval_is_not_accepted(self, store: SessionStore) -> None:
        record = _approved_record(store)
        record.approval = {
            "approved_by": "tester",
            "approved_at": "2026-10-06T00:00:00+00:00",
            "artifact_hashes": {},
        }
        with pytest.raises(ApprovalError, match="not bound to a sealed bundle manifest"):
            require_approved_snapshot(store, record, algorithm="selection", tier=None)
