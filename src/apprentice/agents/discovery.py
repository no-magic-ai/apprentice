"""Discovery Agent — ADK LlmAgent that suggests candidate algorithms."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_CATALOG_PATH: Path = Path(__file__).parent.parent.parent.parent / "config" / "catalog.toml"
_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9_]{1,64}$")

_INSTRUCTION = """\
You are a curriculum designer for the no-magic educational algorithms project.
Your role is to identify well-known, teachable algorithms for a specific learning tier.

Prioritize:
- Clear pedagogical value — the algorithm teaches a transferable concept
- Appropriate complexity for the tier (1=beginner, 2=intermediate, 3=advanced, 4=expert)
- Prerequisites before variants (e.g., insertion sort before shell sort)
- Algorithms with clean, self-contained implementations (no heavy dependencies)

Use the available tools to:
1. Call load_catalog to see existing algorithms (do not suggest duplicates).
2. Generate candidate names and call check_duplicate on each to verify uniqueness.
3. Call validate_name on each candidate to ensure valid naming format.

Return a JSON array where each element has exactly two keys:
  "name": snake_case algorithm name
  "rationale": one sentence explaining pedagogical value

Return ONLY the JSON array, no markdown fences.
"""


def load_catalog() -> dict[str, Any]:
    """Load all algorithm names and aliases from the catalog.

    Returns:
        Dict with 'algorithms' list containing name and tier for each entry,
        and 'all_names' list of all names and aliases for dedup checking.
    """
    if not _CATALOG_PATH.exists():
        return {"algorithms": [], "all_names": []}

    with _CATALOG_PATH.open("rb") as fh:
        data: Any = tomllib.load(fh)

    algorithms: Any = data.get("algorithms", [])
    if not isinstance(algorithms, list):
        return {"algorithms": [], "all_names": []}

    entries: list[dict[str, Any]] = []
    all_names: list[str] = []

    for entry in algorithms:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        tier = entry.get("tier", 0)
        if isinstance(name, str) and name:
            normalized = _normalize_name(name)
            entries.append({"name": normalized, "tier": tier})
            all_names.append(normalized)
        aliases: Any = entry.get("aliases", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, str) and alias:
                    all_names.append(_normalize_name(alias))

    return {"algorithms": entries, "all_names": all_names}


def check_duplicate(candidate_name: str) -> dict[str, Any]:
    """Check whether a candidate algorithm name is too similar to any existing name.

    Uses Levenshtein similarity with a 0.85 threshold.

    Args:
        candidate_name: The candidate algorithm name to check.

    Returns:
        Dict with 'is_duplicate' bool and 'similar_to' name if duplicate found.
    """
    catalog = load_catalog()
    existing = catalog["all_names"]
    normalized = _normalize_name(candidate_name)

    for name in existing:
        norm_name = _normalize_name(name)
        max_len = max(len(normalized), len(norm_name))
        if max_len == 0:
            continue
        distance = _levenshtein_distance(normalized, norm_name)
        similarity = 1.0 - distance / max_len
        if similarity >= 0.85:
            return {"is_duplicate": True, "similar_to": name}

    return {"is_duplicate": False, "similar_to": ""}


def validate_name(name: str) -> dict[str, Any]:
    """Validate that an algorithm name follows naming conventions.

    Names must be lowercase alphanumeric with underscores, max 64 characters.

    Args:
        name: The algorithm name to validate.

    Returns:
        Dict with 'valid' bool and 'normalized' name string.
    """
    normalized = _normalize_name(name)
    valid = bool(_NAME_PATTERN.match(normalized))
    return {"valid": valid, "normalized": normalized}


def build_discovery_agent(model: LiteLlm) -> LlmAgent:
    """Build an ADK LlmAgent for algorithm candidate discovery.

    Uses tools to load catalog, check duplicates, and validate names.

    Args:
        model: LiteLlm model instance.

    Returns:
        A configured LlmAgent with discovery tools.
    """
    return LlmAgent(
        name="discovery",
        model=model,
        instruction=_INSTRUCTION,
        tools=[load_catalog, check_duplicate, validate_name],
        output_key="discovery_candidates",
        description="Suggests candidate algorithms for the no-magic educational project.",
    )


def _normalize_name(name: str) -> str:
    """Normalize an algorithm name for comparison."""
    return name.lower().strip().replace("-", "_").replace(" ", "_")


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
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
