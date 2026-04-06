"""Assessment stage — Anki flashcard deck generation."""

from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.models.budget import CostEstimate
    from apprentice.models.work_item import PipelineContext, StageResult, WorkItem

# Estimated total tokens for card generation (straightforward task)
_TOTAL_TOKENS: int = 3_000

# Hardcoded Sonnet rates (USD per token)
_INPUT_RATE_USD: float = 3.0 / 1_000_000
_OUTPUT_RATE_USD: float = 15.0 / 1_000_000

# 60 % input / 40 % output split for estimates
_INPUT_FRACTION: float = 0.6
_OUTPUT_FRACTION: float = 0.4

# Max tokens ceiling for provider call
_MAX_TOKENS: int = 6_000

# Expected CSV columns per schema
_CSV_COLUMNS: tuple[str, ...] = ("front", "back", "tags", "type")
_MIN_CARDS: int = 8
_CARD_TYPES: tuple[str, ...] = ("concept", "complexity", "implementation", "comparison")


class AssessmentStage:
    """Generate Anki flashcard decks from algorithm implementations.

    Attributes:
        name: Stage identifier used by the pipeline.
    """

    name: str = "assessment"

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
        """Generate, validate, and persist an Anki flashcard CSV.

        Args:
            work_item: Describes the algorithm to generate cards for.
            context: Pipeline-wide configuration and budget state.

        Returns:
            StageResult with the anki_deck artifact path, token usage, cost,
            and diagnostics.
        """
        from apprentice.models.work_item import StageResult

        implementation_path = context.config.get("artifacts", {}).get("implementation", "")
        source_code = _read_implementation(implementation_path)

        prompt = _build_prompt(work_item, source_code)
        completion = _generate(prompt, context)

        csv_content = _extract_csv(completion.text)

        diagnostics: list[dict[str, Any]] = []
        validation_issues = _validate_csv(csv_content)
        if validation_issues:
            diagnostics.extend(
                {"level": "warning", "message": issue} for issue in validation_issues
            )

        artifact_path = _write_artifact(work_item.algorithm_name, csv_content)
        total_tokens = completion.input_tokens + completion.output_tokens
        cost = (
            completion.input_tokens * _INPUT_RATE_USD + completion.output_tokens * _OUTPUT_RATE_USD
        )

        return StageResult(
            stage_name=self.name,
            artifacts={"anki_deck": artifact_path},
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
    """Construct the LLM prompt for Anki card generation.

    Args:
        work_item: Algorithm metadata.
        source_code: Implementation source to base cards on.

    Returns:
        Formatted prompt string.
    """
    source_section = (
        f"\n\n## Implementation Reference\n\n```python\n{source_code}\n```"
        if source_code
        else "\n\n(No implementation provided — generate cards from general knowledge.)"
    )

    card_types_str = ", ".join(f"`{t}`" for t in _CARD_TYPES)

    return (
        f"Generate Anki flashcards for the **{work_item.algorithm_name}** algorithm "
        f"(tier {work_item.tier}).\n"
        f"{source_section}\n\n"
        "## Requirements\n\n"
        f"- Produce at least {_MIN_CARDS} cards total — at least 2 per type.\n"
        f"- Card types: {card_types_str}.\n"
        "  - `concept`: what the algorithm is and how it works conceptually.\n"
        "  - `complexity`: time and space complexity with justification.\n"
        "  - `implementation`: code-level details, edge cases, invariants.\n"
        "  - `comparison`: how this algorithm compares to similar alternatives.\n"
        "- Output format: CSV with a header row and one card per subsequent row.\n"
        "- Columns (in this exact order): `front`, `back`, `tags`, `type`.\n"
        "  - `front`: question or prompt (no commas — use semicolons if needed).\n"
        "  - `back`: answer or explanation.\n"
        "  - `tags`: space-separated tags (e.g. `algorithms sorting`).\n"
        f"  - `type`: one of {card_types_str}.\n"
        "- All fields must be non-empty.\n"
        "- Wrap the CSV in a ```csv ... ``` fence.\n\n"
        "Return **only** the CSV inside a ```csv ... ``` fence."
    )


def _generate(prompt: str, context: PipelineContext) -> _Completion:
    """Invoke the configured provider to generate the flashcard CSV.

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


def _extract_csv(response: str) -> str:
    """Extract CSV content from a markdown fenced block.

    Tries ```csv first, then falls back to a generic ``` fence, then returns
    the raw response if no fence is found.

    Args:
        response: Raw LLM response text.

    Returns:
        CSV content with surrounding whitespace stripped.
    """
    for fence_open in ("```csv", "```"):
        start = response.find(fence_open)
        if start == -1:
            continue
        code_start = start + len(fence_open)
        end = response.find("```", code_start)
        if end == -1:
            return response[code_start:].strip()
        return response[code_start:end].strip()

    return response.strip()


def _validate_csv(content: str) -> list[str]:
    """Validate that CSV content meets the Anki schema requirements.

    Args:
        content: Raw CSV string to validate.

    Returns:
        List of human-readable issue descriptions. Empty list means valid.
    """
    issues: list[str] = []

    if not content.strip():
        issues.append("CSV content is empty")
        return issues

    try:
        reader = csv.DictReader(io.StringIO(content))
        if reader.fieldnames is None:
            issues.append("CSV has no header row")
            return issues

        actual_fields = [f.strip() for f in reader.fieldnames]
        missing = [col for col in _CSV_COLUMNS if col not in actual_fields]
        if missing:
            issues.append(f"CSV missing required columns: {missing}")

        rows = list(reader)
        if len(rows) < _MIN_CARDS:
            issues.append(f"CSV has {len(rows)} cards; minimum is {_MIN_CARDS}")

        for i, row in enumerate(rows, start=2):
            for col in _CSV_COLUMNS:
                val = (row.get(col) or "").strip()
                if not val:
                    issues.append(f"Row {i}: column '{col}' is empty")

    except csv.Error as exc:
        issues.append(f"CSV parse error: {exc}")

    return issues


def _write_artifact(algorithm_name: str, content: str) -> str:
    """Write CSV content to a temp file and return its path string.

    Args:
        algorithm_name: Used as the filename stem.
        content: CSV source to persist.

    Returns:
        Absolute path to the written file as a string.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"{algorithm_name}_cards.csv"
    dest.write_text(content, encoding="utf-8")
    return str(dest)
