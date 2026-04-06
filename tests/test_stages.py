"""Tests for instrumentation, assessment, visualization, and validation stages."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from apprentice.models.work_item import PipelineContext, WorkItem
from apprentice.stages.assessment import AssessmentStage, _extract_csv, _validate_csv
from apprentice.stages.instrumentation import InstrumentationStage
from apprentice.stages.validation import ValidationStage
from apprentice.stages.visualization import VisualizationStage, _to_pascal_case

if TYPE_CHECKING:
    from pathlib import Path


class TestInstrumentationStage:
    def test_name(self) -> None:
        assert InstrumentationStage().name == "instrumentation"

    def test_estimate_cost(self) -> None:
        stage = InstrumentationStage()
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        est = stage.estimate_cost(item)
        assert est.estimated_input_tokens > 0

    def test_execute_requires_provider(self) -> None:
        stage = InstrumentationStage()
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        ctx = PipelineContext(config={"artifacts": {"implementation": "/nonexistent"}})
        with pytest.raises(RuntimeError, match="[Nn]o provider"):
            stage.execute(item, ctx)


class TestAssessmentStage:
    def test_name(self) -> None:
        assert AssessmentStage().name == "assessment"

    def test_estimate_cost(self) -> None:
        stage = AssessmentStage()
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        est = stage.estimate_cost(item)
        assert est.estimated_input_tokens > 0

    def test_execute_requires_provider(self) -> None:
        stage = AssessmentStage()
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        ctx = PipelineContext(config={"artifacts": {"implementation": "/nonexistent"}})
        with pytest.raises(RuntimeError, match="[Nn]o provider"):
            stage.execute(item, ctx)


class TestExtractCsv:
    def test_extracts_csv_fence(self) -> None:
        text = "```csv\nfront,back,tags,type\nQ1,A1,algo,concept\n```"
        assert "front,back" in _extract_csv(text)

    def test_no_fence_returns_raw(self) -> None:
        text = "front,back,tags,type\nQ1,A1,algo,concept"
        assert "front,back" in _extract_csv(text)


class TestValidateCsv:
    def test_valid_csv(self) -> None:
        csv_text = "front,back,tags,type\n" + "\n".join(f"Q{i},A{i},algo,concept" for i in range(8))
        issues = _validate_csv(csv_text)
        assert len(issues) == 0

    def test_too_few_rows(self) -> None:
        csv_text = "front,back,tags,type\nQ1,A1,algo,concept"
        issues = _validate_csv(csv_text)
        assert any("row" in i.lower() or "card" in i.lower() for i in issues)


class TestVisualizationStage:
    def test_name(self) -> None:
        assert VisualizationStage().name == "visualization"

    def test_estimate_cost(self) -> None:
        stage = VisualizationStage()
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        est = stage.estimate_cost(item)
        assert est.estimated_input_tokens > 0

    def test_execute_requires_provider(self) -> None:
        stage = VisualizationStage()
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        ctx = PipelineContext(config={"artifacts": {"implementation": "/nonexistent"}})
        with pytest.raises(RuntimeError, match="[Nn]o provider"):
            stage.execute(item, ctx)


class TestToPascalCase:
    def test_snake_case(self) -> None:
        assert _to_pascal_case("quick_sort") == "QuickSort"

    def test_single_word(self) -> None:
        assert _to_pascal_case("dijkstra") == "Dijkstra"

    def test_hyphenated(self) -> None:
        assert _to_pascal_case("merge-sort") == "MergeSort"


class TestValidationStage:
    def test_name(self) -> None:
        assert ValidationStage().name == "validation"

    def test_estimate_cost_zero_llm(self) -> None:
        stage = ValidationStage()
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        est = stage.estimate_cost(item)
        assert est.estimated_cost_usd == 0.0

    def test_validates_good_implementation(self, tmp_path: Path) -> None:
        impl = tmp_path / "quicksort.py"
        impl.write_text(
            textwrap.dedent('''\
            """Quicksort implementation."""


            def quicksort(arr: list[int]) -> list[int]:
                """Sort a list using quicksort.

                Complexity:
                    Time: O(n log n) average
                    Space: O(n)
                """
                if len(arr) <= 1:
                    return arr
                pivot = arr[0]
                left = [x for x in arr[1:] if x <= pivot]
                right = [x for x in arr[1:] if x > pivot]
                return quicksort(left) + [pivot] + quicksort(right)

            if __name__ == "__main__":
                assert quicksort([3, 1, 2]) == [1, 2, 3]
            ''')
        )

        stage = ValidationStage()
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        ctx = PipelineContext(config={"artifacts": {"implementation": str(impl)}})
        result = stage.execute(item, ctx)
        assert result.tokens_used == 0
        # At least correctness and complexity checks should pass
        passed = [d for d in result.diagnostics if d.get("passed")]
        assert len(passed) >= 2
