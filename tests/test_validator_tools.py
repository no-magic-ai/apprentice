"""Tests for the validator tools bound to one trusted artifact root."""

from __future__ import annotations

import errno
import json
import os
import textwrap
from typing import TYPE_CHECKING

import pytest

from apprentice.sandbox.errors import SandboxUnavailableError, StagingError, UnsafeInputError
from apprentice.sandbox.staging import MAX_ARTIFACT_BYTES
from apprentice.validators.tools import ValidatorTools

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import JudgedSandbox

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

# A sentinel for a credential file outside the root (constructed; not a credential).
_SECRET = "sentinel_" + "c" * 32


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path


@pytest.fixture
def judged(judged_execution: JudgedSandbox) -> JudgedSandbox:
    return judged_execution


@pytest.fixture
def tools(root: Path, judged: JudgedSandbox) -> ValidatorTools:
    return ValidatorTools(root, judged)


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    path = tmp_path / "hosts.yml"
    path.write_text(f"github.com:\n    oauth_token: {_SECRET}\n")
    return path


# Each makes an unsafe file inside the root and returns the path a caller requests.
def _symlink(root: Path, secret: Path) -> str:
    os.symlink(secret, root / "implementation.py")
    return "implementation.py"


def _linked_directory(root: Path, secret: Path) -> str:
    os.symlink(secret.parent, root / "linked")
    return f"linked/{secret.name}"


def _hard_link(root: Path, secret: Path) -> str:
    (root / "other.py").write_text(_GOOD_CODE)
    os.link(root / "other.py", root / "implementation.py")
    return "implementation.py"


def _fifo(root: Path, secret: Path) -> str:
    os.mkfifo(root / "implementation.py")
    return "implementation.py"


def _directory(root: Path, secret: Path) -> str:
    (root / "implementation.py").mkdir()
    return "implementation.py"


def _oversized(root: Path, secret: Path) -> str:
    (root / "implementation.py").write_bytes(b"#" * (MAX_ARTIFACT_BYTES + 1))
    return "implementation.py"


_UNSAFE_SHAPES = {
    "symlink": _symlink,
    "linked directory": _linked_directory,
    "hard link": _hard_link,
    "fifo": _fifo,
    "directory": _directory,
    "oversized": _oversized,
}

_CALLS = {
    "lint": lambda tools, path: tools.lint_validate(path),
    "correctness": lambda tools, path: tools.correctness_validate(path),
    "stdlib": lambda tools, path: tools.stdlib_check(path),
    "consistency": lambda tools, path: tools.consistency_validate(
        json.dumps({"implementation": path}), "algo"
    ),
    "schema": lambda tools, path: tools.schema_validate(json.dumps({"implementation": path})),
}


class TestCorrectnessValidate:
    def test_runs_the_staged_bytes_in_the_sandbox(
        self, root: Path, tools: ValidatorTools, judged: JudgedSandbox
    ) -> None:
        (root / "algo.py").write_text(_GOOD_CODE)

        result = tools.correctness_validate(str(root / "algo.py"))

        assert result["passed"] is True
        assert judged.programs == [_GOOD_CODE.encode()]

    def test_a_failing_program_is_feedback_not_an_error(
        self, root: Path, tools: ValidatorTools
    ) -> None:
        (root / "bad.py").write_text(
            'if __name__ == "__main__":\n    assert False, "fixture: deliberate failure"\n'
        )

        result = tools.correctness_validate("bad.py")

        assert result["passed"] is False
        assert "execution failed" in result["issues"][0]["message"]

    def test_tools_without_a_sandbox_refuse_to_execute(self, root: Path) -> None:
        (root / "algo.py").write_text(_GOOD_CODE)

        with pytest.raises(SandboxUnavailableError):
            ValidatorTools(root).correctness_validate(str(root / "algo.py"))


class TestScope:
    """Tool arguments only ever name files inside the root the caller bound."""

    @pytest.mark.parametrize("form", ["absolute", "relative"])
    def test_paths_outside_the_root_are_refused_unread(
        self, root: Path, tools: ValidatorTools, judged: JudgedSandbox, secret: Path, form: str
    ) -> None:
        path = str(secret) if form == "absolute" else f"../{secret.name}"

        results = [
            tools.lint_validate(path),
            tools.correctness_validate(path),
            tools.stdlib_check(path),
            tools.consistency_validate(json.dumps({"implementation": path}), "algo"),
            tools.schema_validate(json.dumps({"implementation": path})),
        ]

        assert all(result["passed"] is False for result in results)
        assert _SECRET not in json.dumps(results)
        assert judged.programs == []

    @pytest.mark.parametrize("shape", sorted(_UNSAFE_SHAPES))
    @pytest.mark.parametrize("method", sorted(_CALLS))
    def test_unsafe_file_inside_the_root_is_refused_unread_and_unrun(
        self,
        root: Path,
        tools: ValidatorTools,
        judged: JudgedSandbox,
        secret: Path,
        shape: str,
        method: str,
    ) -> None:
        path = _UNSAFE_SHAPES[shape](root, secret)

        result = _CALLS[method](tools, path)

        assert result["passed"] is False
        assert _SECRET not in json.dumps(result)
        assert judged.programs == []

    @pytest.mark.parametrize("method", sorted(_CALLS))
    def test_a_fault_reading_the_root_aborts_instead_of_refusing(
        self, root: Path, tools: ValidatorTools, monkeypatch: pytest.MonkeyPatch, method: str
    ) -> None:
        (root / "implementation.py").write_text(_GOOD_CODE)

        def failing_read(fd: int, size: int) -> bytes:
            raise OSError(errno.EIO, "injected I/O error")

        monkeypatch.setattr("apprentice.sandbox.staging.os.read", failing_read)
        with pytest.raises(StagingError) as excinfo:
            _CALLS[method](tools, "implementation.py")
        assert not isinstance(excinfo.value, UnsafeInputError)


class TestConsistencyValidate:
    def test_algorithm_name_comes_from_caller_not_role_filename(
        self, root: Path, tools: ValidatorTools
    ) -> None:
        f = root / "implementation.py"
        f.write_text(_GOOD_CODE)

        result = tools.consistency_validate(json.dumps({"implementation": str(f)}), "dijkstra")

        messages = [issue["message"] for issue in result["issues"]]
        assert any("'dijkstra'" in message for message in messages)
        assert not any("'implementation'" in message for message in messages)

    def test_malformed_artifact_map_is_refused(self, tools: ValidatorTools) -> None:
        result = tools.consistency_validate(json.dumps(["implementation.py"]), "algo")

        assert result["passed"] is False


class TestStdlibCheck:
    def test_pass_on_stdlib_only(self, root: Path, tools: ValidatorTools) -> None:
        (root / "algo.py").write_text("import os\nimport sys\nprint('hello')\n")
        result = tools.stdlib_check(str(root / "algo.py"))
        assert result["passed"] is True
        assert result["violations"] == []

    def test_fail_on_third_party(self, root: Path, tools: ValidatorTools) -> None:
        (root / "algo.py").write_text("import numpy\nimport os\nprint('hello')\n")
        result = tools.stdlib_check(str(root / "algo.py"))
        assert result["passed"] is False
        assert "numpy" in result["violations"]

    def test_missing_file(self, root: Path, tools: ValidatorTools) -> None:
        result = tools.stdlib_check(str(root / "missing.py"))
        assert result["passed"] is False
        assert "error" in result

    def test_syntax_error(self, root: Path, tools: ValidatorTools) -> None:
        (root / "bad.py").write_text("def foo(:\n  pass")
        result = tools.stdlib_check(str(root / "bad.py"))
        assert result["passed"] is False
        assert "error" in result
