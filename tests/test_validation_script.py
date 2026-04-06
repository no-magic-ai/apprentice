"""Tests for the validation script."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.validate_foundation import (
    analyze_structure,
    compute_similarity,
    validate_file,
    validate_token_usage,
)

if TYPE_CHECKING:
    from pathlib import Path

_SAMPLE_CODE = '''"""Sample algorithm implementation."""

from __future__ import annotations


def quicksort(arr: list[int]) -> list[int]:
    """Sort a list using quicksort.

    Args:
        arr: Input list of integers.

    Returns:
        Sorted list.

    Complexity:
        Time: O(n log n) average
        Space: O(n)
    """
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quicksort(left) + middle + quicksort(right)


if __name__ == "__main__":
    assert quicksort([3, 1, 2]) == [1, 2, 3]
    assert quicksort([]) == []
    assert quicksort([1]) == [1]
'''


class TestAnalyzeStructure:
    def test_counts_functions(self) -> None:
        result = analyze_structure(_SAMPLE_CODE)
        assert result["function_count"] == 1

    def test_detects_main_block(self) -> None:
        result = analyze_structure(_SAMPLE_CODE)
        assert result["has_main_block"] is True

    def test_detects_docstrings(self) -> None:
        result = analyze_structure(_SAMPLE_CODE)
        assert result["has_module_docstring"] is True
        assert result["functions_with_docstrings"] == 1

    def test_detects_type_hints(self) -> None:
        result = analyze_structure(_SAMPLE_CODE)
        assert result["functions_with_type_hints"] == 1


class TestComputeSimilarity:
    def test_identical_structures(self) -> None:
        struct = analyze_structure(_SAMPLE_CODE)
        score = compute_similarity(struct, struct)
        assert score >= 0.85

    def test_empty_vs_populated(self) -> None:
        gen = analyze_structure("x = 1")
        ref = analyze_structure(_SAMPLE_CODE)
        score = compute_similarity(gen, ref)
        assert score < 0.85


class TestValidateTokenUsage:
    def test_within_threshold(self) -> None:
        ok, drift = validate_token_usage(5000, 5000)
        assert ok is True
        assert drift == 0.0

    def test_exceeds_threshold(self) -> None:
        ok, drift = validate_token_usage(7000, 5000)
        assert ok is False
        assert drift > 0.30

    def test_zero_estimate(self) -> None:
        ok, _ = validate_token_usage(0, 0)
        assert ok is True


class TestValidateFile:
    def test_validates_generated_file(self, tmp_path: Path) -> None:
        gen_file = tmp_path / "quicksort.py"
        gen_file.write_text(_SAMPLE_CODE)
        result = validate_file(gen_file)
        assert result["overall_pass"] is True

    def test_validates_with_reference(self, tmp_path: Path) -> None:
        gen_file = tmp_path / "gen.py"
        ref_file = tmp_path / "ref.py"
        gen_file.write_text(_SAMPLE_CODE)
        ref_file.write_text(_SAMPLE_CODE)
        result = validate_file(gen_file, ref_file)
        assert result["similarity_pass"] is True
        assert result["similarity"] >= 0.85
