"""Visualization stage — Manim scene generation from scaffold templates."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apprentice.models.budget import CostEstimate
    from apprentice.models.work_item import PipelineContext, StageResult, WorkItem

# Fixed token budget for Manim code generation (complex output)
_TOTAL_TOKENS: int = 6_000

# Hardcoded Sonnet rates (USD per token) — mirrors implementation.py
_INPUT_RATE_USD: float = 3.0 / 1_000_000
_OUTPUT_RATE_USD: float = 15.0 / 1_000_000

# 60 % input / 40 % output split for estimates
_INPUT_FRACTION: float = 0.6
_OUTPUT_FRACTION: float = 0.4

# Path to the Manim scaffold template, relative to this file's package root
_TEMPLATE_NAME: str = "manim_scene.py.j2"


class VisualizationStage:
    """Generate a Manim scene that visualizes an algorithm's key operations.

    Attributes:
        name: Stage identifier used by the pipeline.
    """

    name: str = "visualization"

    def estimate_cost(self, work_item: WorkItem) -> CostEstimate:
        """Return a pre-execution cost estimate for Manim scene generation.

        Args:
            work_item: The algorithm work item to estimate for.

        Returns:
            CostEstimate with token split and USD cost.
        """
        from apprentice.models.budget import CostEstimate

        input_tokens = int(_TOTAL_TOKENS * _INPUT_FRACTION)
        output_tokens = int(_TOTAL_TOKENS * _OUTPUT_FRACTION)
        cost = input_tokens * _INPUT_RATE_USD + output_tokens * _OUTPUT_RATE_USD
        return CostEstimate(
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost_usd=round(cost, 6),
        )

    def execute(self, work_item: WorkItem, context: PipelineContext) -> StageResult:
        """Generate a complete Manim scene for the algorithm.

        Reads the implementation artifact from context, prompts the LLM for
        animation steps only, renders those into the scaffold template, and
        writes the scene file to a temp directory.

        Args:
            work_item: Describes the algorithm to visualize.
            context: Pipeline-wide configuration and budget state.

        Returns:
            StageResult with the manim_scene artifact path, token usage, cost,
            and diagnostics.
        """
        from apprentice.models.work_item import StageResult

        implementation_code = self._load_implementation(context)
        template_text = self._load_template(context)
        prompt = self._build_prompt(work_item, implementation_code, template_text)
        completion = self._generate(prompt, context)

        animation_steps = _extract_animation_steps(completion.text)
        class_name = _to_pascal_case(work_item.algorithm_name)

        scene_source = _render_template(
            template_text=template_text,
            algorithm_name=work_item.algorithm_name,
            class_name=class_name,
            animation_steps=animation_steps,
        )

        artifact_path = self._write_artifact(work_item.algorithm_name, scene_source)
        total_tokens = completion.input_tokens + completion.output_tokens
        cost = (
            completion.input_tokens * _INPUT_RATE_USD + completion.output_tokens * _OUTPUT_RATE_USD
        )

        return StageResult(
            stage_name=self.name,
            artifacts={"manim_scene": artifact_path},
            tokens_used=total_tokens,
            cost_usd=round(cost, 6),
            diagnostics=[],
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_implementation(self, context: PipelineContext) -> str:
        """Read the implementation artifact path from context config.

        Args:
            context: Pipeline context; reads ``config["implementation_artifact"]``.

        Returns:
            File contents if the path is present and readable, else empty string.
        """
        path_str = context.config.get("implementation_artifact", "")
        if not path_str:
            return ""
        path = Path(str(path_str))
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def _load_template(self, context: PipelineContext) -> str:
        """Locate and read the Manim scaffold template.

        Resolution order:
        1. ``context.config["template_dir"]`` / manim_scene.py.j2
        2. ``<package_root>/../../config/templates/`` / manim_scene.py.j2

        Args:
            context: Pipeline context; reads ``config["template_dir"]`` if set.

        Returns:
            Raw Jinja2 template text.

        Raises:
            FileNotFoundError: If the template cannot be found in either location.
        """
        candidates: list[Path] = []

        template_dir = context.config.get("template_dir", "")
        if template_dir:
            candidates.append(Path(str(template_dir)) / _TEMPLATE_NAME)

        # Derive from this file's location: stages/ → apprentice/ → src/ → project root
        package_root = Path(__file__).parent.parent.parent.parent
        candidates.append(package_root / "config" / "templates" / _TEMPLATE_NAME)

        for candidate in candidates:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")

        raise FileNotFoundError(
            f"Manim scaffold template not found. Searched: {[str(c) for c in candidates]}"
        )

    def _build_prompt(
        self,
        work_item: WorkItem,
        implementation_code: str,
        template_text: str,
    ) -> str:
        """Construct the LLM prompt requesting only animation_steps content.

        Args:
            work_item: Algorithm metadata.
            implementation_code: Existing Python implementation for context.
            template_text: The scaffold template so the LLM sees the structure.

        Returns:
            Formatted prompt string.
        """
        impl_section = ""
        if implementation_code:
            impl_section = (
                f"\n\n## Existing Implementation\n\n```python\n{implementation_code}\n```"
            )

        return (
            f"Generate Manim animation steps for the **{work_item.algorithm_name}** algorithm.\n\n"
            "## Scaffold Template\n\n"
            f"```python\n{template_text}\n```\n"
            f"{impl_section}\n\n"
            "## Requirements\n\n"
            "- Produce ONLY the code that goes inside `construct()`, replacing "
            "`{{ animation_steps }}`.\n"
            "- Visualize the algorithm's key operations: comparisons, swaps, traversals, "
            "insertions, or merges as appropriate.\n"
            "- Use only basic Manim objects: Text, Square, Circle, Arrow, VGroup, "
            "MathTex, DecimalNumber, Line, Dot.\n"
            "- Use the color constants already defined on the class: "
            "self.HIGHLIGHT_COLOR, self.COMPARE_COLOR, self.SWAP_COLOR, self.DONE_COLOR.\n"
            "- Keep each animation step self-contained; use self.play() and self.wait().\n"
            "- No imports — they are already provided by the scaffold.\n"
            "- The code must be indented to match the 8-space depth inside construct().\n\n"
            "Return **only** the animation steps inside a ```python ... ``` fence."
        )

    def _generate(self, prompt: str, context: PipelineContext) -> _Completion:
        """Invoke the configured provider to generate animation steps.

        Args:
            prompt: Fully constructed prompt string.
            context: Pipeline context; reads ``config["provider"]`` for the provider.

        Returns:
            A _Completion with text and token counts.

        Raises:
            RuntimeError: If no provider is configured.
        """
        provider = context.config.get("provider")
        max_tokens = _TOTAL_TOKENS * 2

        if provider is not None and hasattr(provider, "complete"):
            result = provider.complete(prompt, {}, max_tokens)
            return _Completion(
                text=result.text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        raise RuntimeError(
            "No provider configured. Set context.config['provider'] to a ProviderInterface instance."
        )

    def _write_artifact(self, algorithm_name: str, scene_source: str) -> str:
        """Write the rendered Manim scene to a temp file.

        Args:
            algorithm_name: Used as the filename stem.
            scene_source: Complete Python source for the Manim scene.

        Returns:
            Absolute path to the written file as a string.
        """
        tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        dest = tmp_dir / f"{algorithm_name}_scene.py"
        dest.write_text(scene_source, encoding="utf-8")
        return str(dest)


# ---------------------------------------------------------------------------
# Internal data class — avoids importing provider types at runtime
# ---------------------------------------------------------------------------


class _Completion:
    """Thin holder for provider response data."""

    __slots__ = ("input_tokens", "output_tokens", "text")

    def __init__(self, text: str, input_tokens: int, output_tokens: int) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions)
# ---------------------------------------------------------------------------


def _extract_animation_steps(response: str) -> str:
    """Extract animation steps from a markdown fenced code block.

    Args:
        response: Raw LLM response text.

    Returns:
        Animation step code with surrounding whitespace stripped. If no fence
        is found the full response is returned unchanged.
    """
    fence_open = "```python"
    fence_close = "```"

    start = response.find(fence_open)
    if start == -1:
        return response.strip()

    code_start = start + len(fence_open)
    end = response.find(fence_close, code_start)
    if end == -1:
        return response[code_start:].strip()

    return response[code_start:end].strip()


def _to_pascal_case(name: str) -> str:
    """Convert a snake_case or space-separated algorithm name to PascalCase.

    Args:
        name: Algorithm name, e.g. ``"quick_sort"`` or ``"merge sort"``.

    Returns:
        PascalCase string, e.g. ``"QuickSort"``.
    """
    parts = re.split(r"[\s_\-]+", name)
    return "".join(part.capitalize() for part in parts if part)


def _render_template(
    template_text: str,
    algorithm_name: str,
    class_name: str,
    animation_steps: str,
) -> str:
    """Render the Jinja2 scaffold template with the provided variables.

    Args:
        template_text: Raw Jinja2 template source.
        algorithm_name: Human-readable algorithm name for the title.
        class_name: PascalCase class name for the scene class.
        animation_steps: LLM-generated Manim code for construct().

    Returns:
        Fully rendered Python source string.
    """
    from jinja2 import Environment, StrictUndefined

    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    tmpl = env.from_string(template_text)
    return tmpl.render(
        algorithm_name=algorithm_name,
        class_name=class_name,
        animation_steps=animation_steps,
    )
