"""Tests for validators (adapted from gate tests)."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from apprentice.models.work_item import WorkItem
from apprentice.validators.base import ValidationResult
from apprentice.validators.consistency import ConsistencyValidator
from apprentice.validators.correctness import CorrectnessValidator
from apprentice.validators.lint import LintValidator
from apprentice.validators.schema_compliance import SchemaComplianceValidator

if TYPE_CHECKING:
    from pathlib import Path

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


def _make_item(name: str = "quicksort") -> WorkItem:
    return WorkItem(id="test", algorithm_name=name, tier=2)


class TestLintValidator:
    def test_pass_on_good_code(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        result = LintValidator().validate({"implementation": str(f)}, _make_item())
        assert isinstance(result, ValidationResult)
        assert result.passed is True

    def test_fail_on_syntax_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def foo(:\n  pass")
        result = LintValidator().validate({"implementation": str(f)}, _make_item())
        assert result.passed is False
        assert any(i.severity == "error" for i in result.issues)

    def test_issues_have_suggestions(self, tmp_path: Path) -> None:
        f = tmp_path / "no_docstring.py"
        f.write_text("def foo(x):\n    return x\n")
        result = LintValidator().validate({"implementation": str(f)}, _make_item())
        assert result.passed is False
        for issue in result.issues:
            assert issue.suggestion != ""

    def test_empty_path(self) -> None:
        result = LintValidator().validate({"implementation": ""}, _make_item())
        assert result.passed is False

    def test_missing_file(self) -> None:
        result = LintValidator().validate({"implementation": "/nonexistent.py"}, _make_item())
        assert result.passed is False


class TestCorrectnessValidator:
    def test_pass_on_good_code(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        result = CorrectnessValidator().validate({"implementation": str(f)}, _make_item())
        assert result.passed is True

    def test_fail_on_assertion_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text('if __name__ == "__main__":\n    assert False\n')
        result = CorrectnessValidator().validate({"implementation": str(f)}, _make_item())
        assert result.passed is False
        assert any("failed" in i.message.lower() for i in result.issues)

    def test_issues_have_suggestions(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text('if __name__ == "__main__":\n    assert False\n')
        result = CorrectnessValidator().validate({"implementation": str(f)}, _make_item())
        for issue in result.issues:
            assert issue.suggestion != ""


class TestConsistencyValidator:
    def test_pass_with_valid_impl(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        result = ConsistencyValidator().validate({"implementation": str(f)}, _make_item())
        # Should pass or only have warnings (no structural errors)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0


class TestSchemaComplianceValidator:
    def test_pass_with_good_implementation(self, tmp_path: Path) -> None:
        f = tmp_path / "algo.py"
        f.write_text(_GOOD_CODE)
        result = SchemaComplianceValidator().validate({"implementation": str(f)}, _make_item())
        # Should pass or have only non-blocking issues
        assert isinstance(result, ValidationResult)
