"""Implementation stage — single-file Python algorithm generation."""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.models.budget import CostEstimate
    from apprentice.models.work_item import PipelineContext, StageResult, WorkItem

# Tier → total token budget for the stage
_TIER_TOKENS: dict[int, int] = {
    1: 3_000,
    2: 5_000,
    3: 8_000,
    4: 12_000,
}

# Hardcoded Sonnet rates (USD per token) used when provider is unavailable
_INPUT_RATE_USD: float = 3.0 / 1_000_000
_OUTPUT_RATE_USD: float = 15.0 / 1_000_000

# 60 % input / 40 % output split for estimates
_INPUT_FRACTION: float = 0.6
_OUTPUT_FRACTION: float = 0.4


class ImplementationStage:
    """Generate a single-file stdlib-only Python implementation for an algorithm.

    Attributes:
        name: Stage identifier used by the pipeline.
    """

    name: str = "implementation"

    def estimate_cost(self, work_item: WorkItem) -> CostEstimate:
        """Return a pre-execution cost estimate based on tier.

        Args:
            work_item: The algorithm work item to estimate for.

        Returns:
            CostEstimate with token split and USD cost.
        """
        from apprentice.models.budget import CostEstimate

        total = _TIER_TOKENS.get(work_item.tier, _TIER_TOKENS[4])
        input_tokens = int(total * _INPUT_FRACTION)
        output_tokens = int(total * _OUTPUT_FRACTION)
        cost = input_tokens * _INPUT_RATE_USD + output_tokens * _OUTPUT_RATE_USD
        return CostEstimate(
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost_usd=round(cost, 6),
        )

    def execute(self, work_item: WorkItem, context: PipelineContext) -> StageResult:
        """Generate, validate, and persist an algorithm implementation.

        Args:
            work_item: Describes the algorithm to implement.
            context: Pipeline-wide configuration and budget state.

        Returns:
            StageResult with the artifact path, token usage, cost, and diagnostics.
        """
        from apprentice.models.work_item import StageResult

        references = self._load_references(context)
        prompt = self._build_prompt(work_item, references)
        completion = self._generate(prompt, context)

        code = _extract_code_block(completion.text)
        non_stdlib = _check_stdlib_only(code)

        diagnostics: list[dict[str, Any]] = []
        if non_stdlib:
            diagnostics.append(
                {
                    "level": "warning",
                    "message": "non-stdlib imports detected",
                    "imports": non_stdlib,
                }
            )

        artifact_path = self._write_artifact(work_item.algorithm_name, code)
        total_tokens = completion.input_tokens + completion.output_tokens
        cost = (
            completion.input_tokens * _INPUT_RATE_USD + completion.output_tokens * _OUTPUT_RATE_USD
        )

        return StageResult(
            stage_name=self.name,
            artifacts={"implementation": artifact_path},
            tokens_used=total_tokens,
            cost_usd=round(cost, 6),
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_references(self, context: PipelineContext) -> list[str]:
        """Load reference implementation snippets from context config.

        Args:
            context: Pipeline context; reads ``config["references"]`` if present.

        Returns:
            Up to three reference code strings.
        """
        raw = context.config.get("references", [])
        if not isinstance(raw, list):
            return []
        return [str(r) for r in raw[:3]]

    def _build_prompt(self, work_item: WorkItem, references: list[str]) -> str:
        """Construct the LLM prompt for implementation generation.

        Args:
            work_item: Algorithm metadata.
            references: Existing implementation snippets for style matching.

        Returns:
            Formatted prompt string.
        """
        ref_section = ""
        if references:
            formatted = "\n\n---\n\n".join(references)
            ref_section = f"\n\n## Reference Implementations (match this style)\n\n{formatted}"

        return (
            f"Generate a Python implementation of the **{work_item.algorithm_name}** algorithm "
            f"(tier {work_item.tier}).\n\n"
            f"**Rationale / description:** {work_item.rationale or 'N/A'}\n"
            f"{ref_section}\n\n"
            "## Requirements\n\n"
            "- Standard library only — zero third-party imports.\n"
            "- Full type annotations on every function and method.\n"
            "- Google-style docstrings on every public symbol.\n"
            "- Include inline test cases using `doctest` in the module docstring.\n"
            "- Single file, no global mutable state, idempotent functions.\n\n"
            "Return **only** the Python source code inside a ```python ... ``` fence."
        )

    def _generate(self, prompt: str, context: PipelineContext) -> _Completion:
        """Invoke the configured provider to generate the implementation.

        Args:
            prompt: Fully constructed prompt string.
            context: Pipeline context; reads ``config["provider"]`` for the provider instance.

        Returns:
            A _Completion with text and token counts.
        """
        provider = context.config.get("provider")
        max_tokens = _TIER_TOKENS[4] * 2  # generous ceiling; tier 4 always present

        if provider is not None and hasattr(provider, "complete"):
            result = provider.complete(prompt, {}, max_tokens)
            return _Completion(
                text=result.text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        # Provider not wired yet — return a stub so the stage structure is testable.
        raise RuntimeError(
            "No provider configured. Set context.config['provider'] to a ProviderInterface instance."
        )

    def _write_artifact(self, algorithm_name: str, code: str) -> str:
        """Write generated code to a temp file and return its path string.

        Args:
            algorithm_name: Used as the filename stem.
            code: Python source to persist.

        Returns:
            Absolute path to the written file as a string.
        """
        tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        dest = tmp_dir / f"{algorithm_name}.py"
        dest.write_text(code, encoding="utf-8")
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


def _check_stdlib_only(code: str) -> list[str]:
    """Identify non-stdlib top-level imports in Python source.

    Args:
        code: Python source code to analyse.

    Returns:
        List of non-stdlib module names. Empty list means all imports are stdlib.

    Raises:
        SyntaxError: If ``code`` cannot be parsed by ``ast.parse``.
    """
    stdlib: frozenset[str] = frozenset(sys.stdlib_module_names)
    tree = ast.parse(code)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in stdlib:
                    violations.append(root)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root and root not in stdlib:
                violations.append(root)

    # Preserve order, deduplicate
    seen: set[str] = set()
    unique: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique
