"""Tests for FunctionTool validator wrappers."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from apprentice.validators.tools import (
    consistency_validate,
    correctness_validate,
    lint_validate,
    schema_validate,
    stdlib_check,
)

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


class TestLintValidate:
    def test_pass_on_good_code(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        result = lint_validate(str(f))
        assert result["passed"] is True
        assert result["validator_name"] == "lint"

    def test_fail_on_bad_code(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def foo(x):\n    return x\n")
        result = lint_validate(str(f))
        assert result["passed"] is False
        assert len(result["issues"]) > 0

    def test_returns_dict(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        result = lint_validate(str(f))
        assert isinstance(result, dict)
        assert "validator_name" in result
        assert "passed" in result
        assert "issues" in result


class TestCorrectnessValidate:
    def test_pass_on_good_code(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        result = correctness_validate(str(f))
        assert result["passed"] is True

    def test_fail_on_assertion_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text('if __name__ == "__main__":\n    assert False\n')
        result = correctness_validate(str(f))
        assert result["passed"] is False


class TestConsistencyValidate:
    def test_pass_with_impl_only(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        import json

        artifacts = json.dumps({"implementation": str(f)})
        result = consistency_validate(artifacts)
        assert isinstance(result, dict)
        assert result["validator_name"] == "consistency"


class TestSchemaValidate:
    def test_pass_with_good_impl(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        import json

        artifacts = json.dumps({"implementation": str(f)})
        result = schema_validate(artifacts)
        assert isinstance(result, dict)
        assert result["validator_name"] == "schema_compliance"


class TestStdlibCheck:
    def test_pass_on_stdlib_only(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text("import os\nimport sys\nprint('hello')\n")
        result = stdlib_check(str(f))
        assert result["passed"] is True
        assert result["violations"] == []

    def test_fail_on_third_party(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text("import numpy\nimport os\nprint('hello')\n")
        result = stdlib_check(str(f))
        assert result["passed"] is False
        assert "numpy" in result["violations"]

    def test_missing_file(self) -> None:
        result = stdlib_check("/nonexistent/file.py")
        assert result["passed"] is False
        assert "error" in result

    def test_syntax_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def foo(:\n  pass")
        result = stdlib_check(str(f))
        assert result["passed"] is False
        assert "error" in result
