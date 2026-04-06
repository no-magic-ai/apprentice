"""Discovery stage — suggest candidate algorithms for a given tier."""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.models.budget import CostEstimate
    from apprentice.models.work_item import PipelineContext, StageResult, WorkItem

# Fixed token budget for the discovery stage — lightweight compared to implementation
_TOTAL_TOKENS: int = 2_000

# Hardcoded Sonnet rates (USD per token)
_INPUT_RATE_USD: float = 3.0 / 1_000_000
_OUTPUT_RATE_USD: float = 15.0 / 1_000_000

# 60 % input / 40 % output split for estimates
_INPUT_FRACTION: float = 0.6
_OUTPUT_FRACTION: float = 0.4

# Default catalog path relative to this file's package root
_CATALOG_PATH: Path = Path(__file__).parent.parent.parent.parent / "config" / "catalog.toml"

# Valid algorithm name pattern: lowercase alphanumeric + underscores, max 64 chars
_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9_]{1,64}$")

# Default number of candidates to request per discovery run
_DEFAULT_LIMIT: int = 5


class DiscoveryStage:
    """Suggest non-duplicate algorithm candidates for a given tier.

    Attributes:
        name: Stage identifier used by the pipeline.
    """

    name: str = "discovery"

    def estimate_cost(self, work_item: WorkItem) -> CostEstimate:
        """Return a pre-execution cost estimate for the discovery stage.

        Args:
            work_item: The work item triggering discovery (tier is read for context).

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
        """Run algorithm discovery for the work item's tier.

        Loads the catalog, asks the configured provider for candidate algorithm
        names, deduplicates against existing entries, and writes a JSON artifact
        listing accepted candidates with rationale.

        Args:
            work_item: Supplies the tier to target. ``algorithm_name`` and
                       ``rationale`` are used as context hints for the LLM.
            context: Pipeline-wide config including the provider instance and
                     optional ``catalog_path`` override.

        Returns:
            StageResult with a ``"candidates"`` artifact path, token usage,
            cost, and diagnostics for any rejected or invalid names.

        Raises:
            RuntimeError: If no provider is configured in ``context.config``.
        """
        from apprentice.models.work_item import StageResult

        catalog_path = Path(context.config.get("catalog_path", str(_CATALOG_PATH)))
        existing_names = _load_catalog_names(catalog_path)

        limit: int = int(context.config.get("discovery_limit", _DEFAULT_LIMIT))
        prompt = self._build_prompt(work_item, existing_names, limit)
        completion = self._generate(prompt, context)

        raw_candidates = _parse_candidates(completion.text)

        diagnostics: list[dict[str, Any]] = []
        accepted: list[dict[str, str]] = []

        for entry in raw_candidates:
            name = entry.get("name", "")
            rationale = entry.get("rationale", "")

            normalized = _normalize_name(name)
            if not _NAME_PATTERN.match(normalized):
                diagnostics.append(
                    {
                        "level": "warning",
                        "message": "invalid algorithm name rejected",
                        "name": name,
                    }
                )
                continue

            if _is_duplicate(normalized, existing_names):
                diagnostics.append(
                    {
                        "level": "info",
                        "message": "duplicate candidate filtered",
                        "name": normalized,
                    }
                )
                continue

            accepted.append({"name": normalized, "rationale": rationale})

        artifact_path = self._write_artifact(work_item.algorithm_name, accepted)
        total_tokens = completion.input_tokens + completion.output_tokens
        cost = (
            completion.input_tokens * _INPUT_RATE_USD + completion.output_tokens * _OUTPUT_RATE_USD
        )

        return StageResult(
            stage_name=self.name,
            artifacts={"candidates": artifact_path},
            tokens_used=total_tokens,
            cost_usd=round(cost, 6),
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        work_item: WorkItem,
        existing_names: list[str],
        limit: int,
    ) -> str:
        """Construct the discovery prompt.

        Args:
            work_item: Provides tier and optional context hints.
            existing_names: Names already in the catalog (for exclusion).
            limit: Maximum number of candidates to request.

        Returns:
            Formatted prompt string.
        """
        existing_section = ", ".join(existing_names) if existing_names else "none"
        return (
            f"Suggest {limit} algorithm candidates suitable for tier {work_item.tier} "
            f"of an educational algorithms project.\n\n"
            f"**Context hint:** {work_item.rationale or 'N/A'}\n\n"
            f"**Already in catalog (do not repeat):** {existing_section}\n\n"
            "## Requirements\n\n"
            "- Each candidate must be a well-known, self-contained algorithm.\n"
            "- Choose algorithms with clear pedagogical value at this tier.\n"
            "- Prefer prerequisites before advanced variants.\n\n"
            "Return a JSON array where each element has exactly two keys:\n"
            '  "name": snake_case algorithm name (lowercase, underscores only)\n'
            '  "rationale": one sentence explaining pedagogical value\n\n'
            "Return **only** the JSON array inside a ```json ... ``` fence."
        )

    def _generate(self, prompt: str, context: PipelineContext) -> _Completion:
        """Invoke the configured provider to generate candidate suggestions.

        Args:
            prompt: Fully constructed prompt string.
            context: Pipeline context; reads ``config["provider"]``.

        Returns:
            A _Completion with text and token counts.

        Raises:
            RuntimeError: If no provider is configured.
        """
        provider = context.config.get("provider")

        if provider is not None and hasattr(provider, "complete"):
            result = provider.complete(prompt, {}, _TOTAL_TOKENS * 2)
            return _Completion(
                text=result.text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        raise RuntimeError(
            "No provider configured. Set context.config['provider'] to a ProviderInterface instance."
        )

    def _write_artifact(self, context_name: str, candidates: list[dict[str, str]]) -> str:
        """Persist the accepted candidates as a JSON file.

        Args:
            context_name: Used as a stem for the output filename.
            candidates: List of accepted candidate dicts with name and rationale.

        Returns:
            Absolute path to the written JSON file as a string.
        """
        tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        dest = tmp_dir / f"{context_name}_discovery.json"
        dest.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
        return str(dest)


# ---------------------------------------------------------------------------
# Internal data class
# ---------------------------------------------------------------------------


class _Completion:
    """Thin holder for provider response data."""

    __slots__ = ("input_tokens", "output_tokens", "text")

    def __init__(self, text: str, input_tokens: int, output_tokens: int) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _load_catalog_names(catalog_path: Path) -> list[str]:
    """Load all algorithm names and aliases from the catalog TOML.

    Args:
        catalog_path: Absolute path to ``catalog.toml``.

    Returns:
        Flat list of normalized names and aliases found in the catalog.

    Raises:
        FileNotFoundError: If ``catalog_path`` does not exist.
        ValueError: If the catalog is malformed (missing ``algorithms`` key).
    """
    if not catalog_path.exists():
        raise FileNotFoundError(f"Catalog not found: {catalog_path}")

    with catalog_path.open("rb") as fh:
        data: Any = tomllib.load(fh)

    algorithms: Any = data.get("algorithms", [])
    if not isinstance(algorithms, list):
        raise ValueError(f"Expected 'algorithms' to be a list in {catalog_path}")

    names: list[str] = []
    for entry in algorithms:
        if not isinstance(entry, dict):
            continue
        raw_name = entry.get("name", "")
        if isinstance(raw_name, str) and raw_name:
            names.append(_normalize_name(raw_name))
        aliases: Any = entry.get("aliases", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, str) and alias:
                    names.append(_normalize_name(alias))

    return names


def _parse_candidates(response: str) -> list[dict[str, str]]:
    """Extract the JSON candidate array from a fenced LLM response.

    Args:
        response: Raw LLM response text.

    Returns:
        List of candidate dicts. Each dict has at least a ``"name"`` key.
        Returns an empty list if parsing fails.
    """
    fence_open = "```json"
    fence_close = "```"

    start = response.find(fence_open)
    if start == -1:
        json_text = response.strip()
    else:
        code_start = start + len(fence_open)
        end = response.find(fence_close, code_start)
        json_text = response[code_start:end].strip() if end != -1 else response[code_start:].strip()

    try:
        parsed: Any = json.loads(json_text)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(parsed, list):
        return []

    result: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        rationale = item.get("rationale", "")
        if isinstance(name, str) and name:
            result.append({"name": name, "rationale": str(rationale)})

    return result


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings.

    Args:
        s1: First string.
        s2: Second string.

    Returns:
        Minimum number of single-character edits to transform s1 into s2.
    """
    m, n = len(s1), len(s2)
    dp: list[list[int]] = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if s1[i - 1] == s2[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])

    return dp[m][n]


def _normalize_name(name: str) -> str:
    """Normalize an algorithm name for fuzzy comparison.

    Lowercases, strips whitespace, and replaces hyphens and spaces with
    underscores.

    Args:
        name: Raw algorithm name string.

    Returns:
        Normalized name safe for comparison and catalog insertion.
    """
    return name.lower().strip().replace("-", "_").replace(" ", "_")


def _is_duplicate(
    candidate: str,
    existing: list[str],
    threshold: float = 0.85,
) -> bool:
    """Check whether a candidate name is too similar to any existing name.

    Uses normalized Levenshtein similarity:
    ``similarity = 1 - distance / max(len(a), len(b))``.

    Args:
        candidate: Normalized candidate name.
        existing: List of normalized existing names.
        threshold: Similarity threshold at or above which names are considered
                   duplicates. Defaults to 0.85.

    Returns:
        True if any existing name has similarity >= threshold.
    """
    norm_candidate = _normalize_name(candidate)
    for name in existing:
        norm_name = _normalize_name(name)
        max_len = max(len(norm_candidate), len(norm_name))
        if max_len == 0:
            continue
        distance = _levenshtein_distance(norm_candidate, norm_name)
        similarity = 1.0 - distance / max_len
        if similarity >= threshold:
            return True
    return False
