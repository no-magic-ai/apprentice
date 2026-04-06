"""Tests for prompt template loading and rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from apprentice.prompts.loader import PromptTemplate, load_prompt, render_prompt

_PROMPTS_DIR = Path(__file__).parent.parent / "src" / "apprentice" / "prompts"


class TestLoadPrompt:
    def test_loads_implementation_prompt(self) -> None:
        template = load_prompt("implementation", _PROMPTS_DIR)
        assert template.name == "implementation"
        assert template.version == "1.0.0"
        assert "algorithm_name" in template.variables

    def test_missing_prompt_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_prompt("nonexistent", _PROMPTS_DIR)

    def test_prompt_has_required_fields(self) -> None:
        template = load_prompt("implementation", _PROMPTS_DIR)
        assert template.system_prompt
        assert template.user_prompt_template
        assert len(template.variables) > 0


class TestRenderPrompt:
    def test_renders_with_variables(self) -> None:
        template = load_prompt("implementation", _PROMPTS_DIR)
        system, user = render_prompt(
            template,
            {
                "algorithm_name": "quickselect",
                "tier": 2,
                "description": "Selection algorithm",
                "reference_implementations": [],
            },
        )
        assert "quickselect" in user
        assert system  # non-empty

    def test_missing_variable_raises(self) -> None:
        template = load_prompt("implementation", _PROMPTS_DIR)
        with pytest.raises(ValueError, match="Missing required"):
            render_prompt(template, {"algorithm_name": "quicksort"})

    def test_renders_with_references(self) -> None:
        template = load_prompt("implementation", _PROMPTS_DIR)
        refs = [{"name": "bubble_sort", "code": "def bubble_sort(arr): ..."}]
        _, user = render_prompt(
            template,
            {
                "algorithm_name": "insertion_sort",
                "tier": 1,
                "description": "Simple sorting",
                "reference_implementations": refs,
            },
        )
        assert "bubble_sort" in user


class TestPromptTemplateDataclass:
    def test_fields(self) -> None:
        pt = PromptTemplate(
            name="test",
            version="0.1.0",
            system_prompt="sys",
            user_prompt_template="user {{ x }}",
            variables=["x"],
        )
        assert pt.name == "test"
        assert pt.variables == ["x"]
