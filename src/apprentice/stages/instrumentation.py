"""Instrumentation stage — adds trace hooks for step-by-step replay."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.models.budget import CostEstimate
    from apprentice.models.work_item import PipelineContext, StageResult, WorkItem

# Estimated total tokens for instrumentation (slightly less than implementation)
_TOTAL_TOKENS: int = 4_000

# Hardcoded Sonnet rates (USD per token)
_INPUT_RATE_USD: float = 3.0 / 1_000_000
_OUTPUT_RATE_USD: float = 15.0 / 1_000_000

# 60 % input / 40 % output split for estimates
_INPUT_FRACTION: float = 0.6
_OUTPUT_FRACTION: float = 0.4

# Max tokens ceiling for provider call
_MAX_TOKENS: int = 8_000


class InstrumentationStage:
    """Add JSON trace hooks to an existing algorithm implementation.

    Attributes:
        name: Stage identifier used by the pipeline.
    """

    name: str = "instrumentation"

    def estimate_cost(self, work_item: WorkItem) -> CostEstimate:
        """Return a pre-execution cost estimate.

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
        """Instrument an implementation with trace hooks and persist the result.

        Args:
            work_item: Describes the algorithm being instrumented.
            context: Pipeline-wide configuration and budget state.

        Returns:
            StageResult with the instrumented artifact path, token usage, cost,
            and diagnostics.
        """
        from apprentice.models.work_item import StageResult

        implementation_path = context.config.get("artifacts", {}).get("implementation", "")
        source_code = _read_implementation(implementation_path)

        prompt = _build_prompt(work_item, source_code)
        completion = _generate(prompt, context)

        instrumented_code = _extract_code_block(completion.text)

        diagnostics: list[dict[str, Any]] = []
        if not instrumented_code.strip():
            diagnostics.append(
                {
                    "level": "warning",
                    "message": "extracted instrumented code is empty; using raw response",
                }
            )
            instrumented_code = completion.text

        artifact_path = _write_artifact(work_item.algorithm_name, instrumented_code)
        total_tokens = completion.input_tokens + completion.output_tokens
        cost = (
            completion.input_tokens * _INPUT_RATE_USD + completion.output_tokens * _OUTPUT_RATE_USD
        )

        return StageResult(
            stage_name=self.name,
            artifacts={"instrumented": artifact_path},
            tokens_used=total_tokens,
            cost_usd=round(cost, 6),
            diagnostics=diagnostics,
        )


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


def _read_implementation(path: str) -> str:
    """Read implementation source from disk.

    Args:
        path: Absolute path to the implementation file.

    Returns:
        File contents as a string, or empty string if path is empty or missing.
    """
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def _build_prompt(work_item: WorkItem, source_code: str) -> str:
    """Construct the LLM prompt for instrumentation.

    Args:
        work_item: Algorithm metadata.
        source_code: Existing implementation to instrument.

    Returns:
        Formatted prompt string.
    """
    source_section = (
        f"\n\n## Existing Implementation\n\n```python\n{source_code}\n```"
        if source_code
        else "\n\n(No existing implementation provided — generate from scratch with tracing.)"
    )

    return (
        f"Instrument the **{work_item.algorithm_name}** algorithm implementation "
        f"with JSON structured trace hooks.\n"
        f"{source_section}\n\n"
        "## Requirements\n\n"
        "- At every significant decision point (comparisons, swaps, splits, merges, "
        "boundary checks), append a trace entry to a module-level `_trace` list.\n"
        "- Each trace entry must be a dict with exactly these keys:\n"
        '  - `"step"`: integer counter, starting at 1, incremented for every entry.\n'
        '  - `"operation"`: short string describing the operation (e.g. `"compare"`, '
        '`"swap"`, `"split"`, `"merge"`).\n'
        '  - `"state"`: dict capturing the relevant local state at that point '
        "(e.g. indices, values, partial results).\n"
        "- At the end of the main algorithm function, print `_trace` as JSON using "
        "`import json; print(json.dumps(_trace, indent=2))`.\n"
        "- Reset `_trace = []` at the start of each top-level algorithm call so the "
        "function is reentrant.\n"
        "- Preserve all original logic, type annotations, and docstrings.\n"
        "- Standard library only — zero third-party imports.\n\n"
        "Return **only** the instrumented Python source code inside a ```python ... ``` fence."
    )


def _generate(prompt: str, context: PipelineContext) -> _Completion:
    """Invoke the configured provider to generate the instrumented code.

    Args:
        prompt: Fully constructed prompt string.
        context: Pipeline context; reads ``config["provider"]`` for the provider instance.

    Returns:
        A _Completion with text and token counts.

    Raises:
        RuntimeError: If no provider is configured.
    """
    provider = context.config.get("provider")

    if provider is not None and hasattr(provider, "complete"):
        result = provider.complete(prompt, {}, _MAX_TOKENS)
        return _Completion(
            text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    raise RuntimeError(
        "No provider configured. Set context.config['provider'] to a ProviderInterface instance."
    )


def _extract_code_block(response: str) -> str:
    """Extract Python source from a markdown fenced code block.

    Args:
        response: Raw LLM response text.

    Returns:
        Python source with surrounding whitespace stripped. If no fence is
        found the full response is returned unchanged.
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


def _write_artifact(algorithm_name: str, code: str) -> str:
    """Write instrumented code to a temp file and return its path string.

    Args:
        algorithm_name: Used as the filename stem.
        code: Python source to persist.

    Returns:
        Absolute path to the written file as a string.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"{algorithm_name}_instrumented.py"
    dest.write_text(code, encoding="utf-8")
    return str(dest)
