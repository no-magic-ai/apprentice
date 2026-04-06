"""Tests for the implementation stage."""

from __future__ import annotations

from apprentice.models.budget import CostEstimate
from apprentice.models.work_item import PipelineContext, WorkItem
from apprentice.stages.implementation import (
    ImplementationStage,
    _check_stdlib_only,
    _extract_code_block,
)


class TestExtractCodeBlock:
    def test_extracts_python_fence(self) -> None:
        response = '```python\nprint("hello")\n```'
        assert _extract_code_block(response) == 'print("hello")'

    def test_extracts_first_fence(self) -> None:
        response = "Some text\n```python\nx = 1\n```\nMore text\n```python\ny = 2\n```"
        assert _extract_code_block(response) == "x = 1"

    def test_no_fence_returns_raw(self) -> None:
        response = "def foo(): pass"
        assert _extract_code_block(response) == "def foo(): pass"

    def test_strips_whitespace(self) -> None:
        response = "```python\n  x = 1  \n```"
        assert _extract_code_block(response) == "x = 1"


class TestCheckStdlibOnly:
    def test_stdlib_only_passes(self) -> None:
        code = (
            "import os\nimport sys\nfrom pathlib import Path\nfrom collections import defaultdict"
        )
        assert _check_stdlib_only(code) == []

    def test_detects_third_party(self) -> None:
        code = "import numpy\nfrom pandas import DataFrame"
        violations = _check_stdlib_only(code)
        assert "numpy" in violations
        assert "pandas" in violations

    def test_empty_code(self) -> None:
        assert _check_stdlib_only("x = 1") == []


class TestImplementationStage:
    def test_name(self) -> None:
        stage = ImplementationStage()
        assert stage.name == "implementation"

    def test_estimate_cost_tier2(self) -> None:
        stage = ImplementationStage()
        item = WorkItem(id="t", algorithm_name="quicksort", tier=2)
        estimate = stage.estimate_cost(item)
        assert isinstance(estimate, CostEstimate)
        assert estimate.estimated_input_tokens > 0
        assert estimate.estimated_output_tokens > 0
        assert estimate.estimated_cost_usd > 0

    def test_estimate_cost_scales_by_tier(self) -> None:
        stage = ImplementationStage()
        est1 = stage.estimate_cost(WorkItem(id="t", algorithm_name="a", tier=1))
        est4 = stage.estimate_cost(WorkItem(id="t", algorithm_name="a", tier=4))
        assert est4.estimated_input_tokens > est1.estimated_input_tokens

    def test_execute_requires_provider(self) -> None:
        import pytest

        stage = ImplementationStage()
        item = WorkItem(id="t", algorithm_name="test", tier=1)
        ctx = PipelineContext(config={})
        with pytest.raises(RuntimeError, match="No provider configured"):
            stage.execute(item, ctx)
