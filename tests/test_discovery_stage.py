"""Tests for the discovery stage."""

from __future__ import annotations

from apprentice.stages.discovery import (
    DiscoveryStage,
    _is_duplicate,
    _levenshtein_distance,
    _normalize_name,
)


class TestNormalizeName:
    def test_lowercase(self) -> None:
        assert _normalize_name("QuickSort") == "quicksort"

    def test_spaces_to_underscores(self) -> None:
        assert _normalize_name("bubble sort") == "bubble_sort"

    def test_hyphens_to_underscores(self) -> None:
        assert _normalize_name("merge-sort") == "merge_sort"

    def test_strips_whitespace(self) -> None:
        assert _normalize_name("  dijkstra  ") == "dijkstra"


class TestLevenshteinDistance:
    def test_identical(self) -> None:
        assert _levenshtein_distance("abc", "abc") == 0

    def test_single_edit(self) -> None:
        assert _levenshtein_distance("abc", "ab") == 1

    def test_empty(self) -> None:
        assert _levenshtein_distance("", "abc") == 3
        assert _levenshtein_distance("abc", "") == 3

    def test_completely_different(self) -> None:
        assert _levenshtein_distance("abc", "xyz") == 3


class TestIsDuplicate:
    def test_exact_match(self) -> None:
        assert _is_duplicate("quicksort", ["quicksort", "bubblesort"])

    def test_high_similarity(self) -> None:
        assert _is_duplicate("quick_sort", ["quicksort"])

    def test_no_match(self) -> None:
        assert not _is_duplicate("dijkstra", ["quicksort", "bubblesort"])

    def test_alias_match(self) -> None:
        assert _is_duplicate("bubblesort", ["bubble_sort"])


class TestDiscoveryStage:
    def test_name(self) -> None:
        stage = DiscoveryStage()
        assert stage.name == "discovery"

    def test_estimate_cost(self) -> None:
        from apprentice.models.work_item import WorkItem

        stage = DiscoveryStage()
        item = WorkItem(id="t", algorithm_name="discovery", tier=2)
        estimate = stage.estimate_cost(item)
        assert estimate.estimated_input_tokens > 0
        assert estimate.estimated_cost_usd > 0
