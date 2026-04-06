"""Validation stage — correctness, render, and format verification."""

from __future__ import annotations

import ast
import csv
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.models.budget import CostEstimate
    from apprentice.models.work_item import PipelineContext, StageResult, WorkItem

# Fixed overhead token estimate (no LLM calls, just accounting for context overhead)
_OVERHEAD_TOKENS: int = 500

# Valid Anki card type values per no-magic-schema.yaml
_VALID_CARD_TYPES: frozenset[str] = frozenset(
    {"concept", "complexity", "implementation", "comparison"}
)

# Minimum rows required in CSV (header + 3 data rows)
_CSV_MIN_ROWS: int = 4

# Expected field count per CSV row
_CSV_FIELD_COUNT: int = 4

# Compiled regex patterns for trace key detection
_TRACE_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'["\']step["\']'),
    re.compile(r'["\']operation["\']'),
    re.compile(r'["\']state["\']'),
)


class ValidationStage:
    """Run local checks against all generated artifacts.

    No LLM calls are made. All checks are purely local: subprocess execution,
    AST parsing, and file content inspection.

    Attributes:
        name: Stage identifier used by the pipeline.
    """

    name: str = "validation"

    def estimate_cost(self, work_item: WorkItem) -> CostEstimate:
        """Return a fixed overhead estimate — validation makes no LLM calls.

        Args:
            work_item: The algorithm work item (unused; included for interface conformance).

        Returns:
            CostEstimate with ~500 tokens overhead and zero USD cost.
        """
        from apprentice.models.budget import CostEstimate

        return CostEstimate(
            estimated_input_tokens=_OVERHEAD_TOKENS,
            estimated_output_tokens=0,
            estimated_cost_usd=0.0,
        )

    def execute(self, work_item: WorkItem, context: PipelineContext) -> StageResult:
        """Run all validation checks and return a StageResult with a JSON report.

        Checks performed:
        - Correctness: execute the implementation file and verify exit code 0.
        - Complexity documentation: parse implementation AST and verify docstring
          contains a Complexity section.
        - Manim render (light): AST-parse the scene file for structural requirements.
        - Anki format: verify CSV structure and card type values.
        - Trace format: search instrumented file for required trace keys.

        Args:
            work_item: The algorithm work item being validated.
            context: Pipeline-wide configuration; reads ``config["artifacts"]``.

        Returns:
            StageResult with ``validation_report`` artifact path, full diagnostics,
            zero tokens used, and zero USD cost.
        """
        from apprentice.models.work_item import StageResult

        artifacts_cfg: dict[str, str] = {}
        raw = context.config.get("artifacts", {})
        if isinstance(raw, dict):
            artifacts_cfg = {str(k): str(v) for k, v in raw.items()}

        impl_path = artifacts_cfg.get("implementation")
        instrumented_path = artifacts_cfg.get("instrumented")
        manim_path = artifacts_cfg.get("manim_scene")
        anki_path = artifacts_cfg.get("anki_deck")

        diagnostics: list[dict[str, Any]] = [
            _check_correctness(impl_path),
            _check_complexity_docs(impl_path),
            _check_manim_structure(manim_path),
            _check_anki_format(anki_path),
            _check_trace_format(instrumented_path),
        ]

        report_path = _write_report(work_item.algorithm_name, diagnostics)

        return StageResult(
            stage_name=self.name,
            artifacts={"validation_report": report_path},
            tokens_used=0,
            cost_usd=0.0,
            diagnostics=diagnostics,
        )


# ---------------------------------------------------------------------------
# Check implementations (pure module-level functions)
# ---------------------------------------------------------------------------


def _check_correctness(path: str | None) -> dict[str, Any]:
    """Execute the implementation file and check for exit code 0.

    Args:
        path: Absolute path to the implementation .py file, or None if absent.

    Returns:
        Diagnostic dict with ``check``, ``passed``, and ``details`` keys.
    """
    check_name = "correctness"

    if path is None or not Path(path).exists():
        return {
            "check": check_name,
            "passed": False,
            "details": "implementation file not found",
            "path": path,
        }

    result = subprocess.run(
        [sys.executable, path],
        capture_output=True,
        timeout=10,
        text=True,
    )

    passed = result.returncode == 0
    return {
        "check": check_name,
        "passed": passed,
        "details": "exit code 0" if passed else f"exit code {result.returncode}",
        "stderr": result.stderr.strip() if result.stderr else None,
        "path": path,
    }


def _check_complexity_docs(path: str | None) -> dict[str, Any]:
    """Verify the main function's docstring contains a Complexity section.

    Searches for "complexity", "time:", "space:", or "O(" (case-insensitive)
    in the first function-level docstring found in the module.

    Args:
        path: Absolute path to the implementation .py file, or None if absent.

    Returns:
        Diagnostic dict with ``check``, ``passed``, and ``details`` keys.
    """
    check_name = "complexity_documentation"

    if path is None or not Path(path).exists():
        return {
            "check": check_name,
            "passed": False,
            "details": "implementation file not found",
            "path": path,
        }

    source = Path(path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {
            "check": check_name,
            "passed": False,
            "details": f"syntax error: {exc}",
            "path": path,
        }

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node) or ""
            lower = docstring.lower()
            if "complexity" in lower or "time:" in lower or "space:" in lower or "o(" in lower:
                return {
                    "check": check_name,
                    "passed": True,
                    "details": f"complexity section found in '{node.name}'",
                    "path": path,
                }

    return {
        "check": check_name,
        "passed": False,
        "details": "no function docstring with complexity section found",
        "path": path,
    }


def _check_manim_structure(path: str | None) -> dict[str, Any]:
    """AST-parse the Manim scene file and verify structural requirements.

    Checks:
    - No syntax errors.
    - At least one import from manim.
    - At least one class with ``Scene`` in its bases.
    - That class defines a ``construct`` method.

    Args:
        path: Absolute path to the Manim scene .py file, or None if absent.

    Returns:
        Diagnostic dict with ``check``, ``passed``, and ``details`` keys.
    """
    check_name = "manim_structure"

    if path is None or not Path(path).exists():
        return {
            "check": check_name,
            "passed": False,
            "details": "manim scene file not found",
            "path": path,
        }

    source = Path(path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {
            "check": check_name,
            "passed": False,
            "details": f"syntax error: {exc}",
            "path": path,
        }

    if not _has_manim_import(tree):
        return {
            "check": check_name,
            "passed": False,
            "details": "no import from manim found",
            "path": path,
        }

    scene_class = _find_scene_class(tree)
    if scene_class is None:
        return {
            "check": check_name,
            "passed": False,
            "details": "no class with Scene in bases found",
            "path": path,
        }

    has_construct = any(
        isinstance(n, ast.FunctionDef) and n.name == "construct"
        for n in ast.walk(scene_class)
        if n is not scene_class
    )
    if not has_construct:
        return {
            "check": check_name,
            "passed": False,
            "details": f"Scene class '{scene_class.name}' has no construct method",
            "path": path,
        }

    return {
        "check": check_name,
        "passed": True,
        "details": f"scene class '{scene_class.name}' with construct method found",
        "path": path,
    }


def _check_anki_format(path: str | None) -> dict[str, Any]:
    """Verify CSV structure: row count, field count, and valid card types.

    Args:
        path: Absolute path to the Anki CSV file, or None if absent.

    Returns:
        Diagnostic dict with ``check``, ``passed``, and ``details`` keys.
    """
    check_name = "anki_format"

    if path is None or not Path(path).exists():
        return {
            "check": check_name,
            "passed": False,
            "details": "anki CSV file not found",
            "path": path,
        }

    text = Path(path).read_text(encoding="utf-8")
    reader = csv.reader(text.splitlines())
    rows = list(reader)

    if len(rows) < _CSV_MIN_ROWS:
        return {
            "check": check_name,
            "passed": False,
            "details": (
                f"expected at least {_CSV_MIN_ROWS} rows (header + 3 data), got {len(rows)}"
            ),
            "path": path,
        }

    malformed = [i for i, row in enumerate(rows) if len(row) != _CSV_FIELD_COUNT]
    if malformed:
        return {
            "check": check_name,
            "passed": False,
            "details": f"rows with wrong field count (expected {_CSV_FIELD_COUNT}): {malformed}",
            "path": path,
        }

    header = [h.strip().lower() for h in rows[0]]
    if "type" not in header:
        return {
            "check": check_name,
            "passed": False,
            "details": "header row missing 'type' column",
            "path": path,
        }

    type_col = header.index("type")
    invalid_types = [
        row[type_col].strip() for row in rows[1:] if row[type_col].strip() not in _VALID_CARD_TYPES
    ]
    if invalid_types:
        return {
            "check": check_name,
            "passed": False,
            "details": f"invalid card type values: {invalid_types}",
            "path": path,
        }

    return {
        "check": check_name,
        "passed": True,
        "details": f"{len(rows) - 1} data rows, all types valid",
        "path": path,
    }


def _check_trace_format(path: str | None) -> dict[str, Any]:
    """Search the instrumented file source for required trace keys.

    Looks for string literals 'step', 'operation', and 'state' used as dict
    keys (regex patterns against raw source).

    Args:
        path: Absolute path to the instrumented .py file, or None if absent.

    Returns:
        Diagnostic dict with ``check``, ``passed``, and ``details`` keys.
    """
    check_name = "trace_format"

    if path is None or not Path(path).exists():
        return {
            "check": check_name,
            "passed": False,
            "details": "instrumented file not found",
            "path": path,
        }

    source = Path(path).read_text(encoding="utf-8")
    missing = [pat.pattern for pat in _TRACE_KEY_PATTERNS if not pat.search(source)]

    if missing:
        return {
            "check": check_name,
            "passed": False,
            "details": f"missing trace key patterns: {missing}",
            "path": path,
        }

    return {
        "check": check_name,
        "passed": True,
        "details": "all required trace keys (step, operation, state) found",
        "path": path,
    }


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def _write_report(algorithm_name: str, diagnostics: list[dict[str, Any]]) -> str:
    """Serialize diagnostics to a JSON file in the temp artifacts directory.

    Args:
        algorithm_name: Used as the report filename stem.
        diagnostics: List of check result dicts.

    Returns:
        Absolute path to the written JSON report as a string.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"{algorithm_name}_validation_report.json"
    dest.write_text(
        json.dumps({"diagnostics": diagnostics}, indent=2),
        encoding="utf-8",
    )
    return str(dest)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _has_manim_import(tree: ast.Module) -> bool:
    """Return True if the module imports anything from manim.

    Args:
        tree: Parsed AST of the module.

    Returns:
        True when at least one ``import manim`` or ``from manim`` node exists.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "manim" or alias.name.startswith("manim."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "manim" or module.startswith("manim."):
                return True
    return False


def _find_scene_class(tree: ast.Module) -> ast.ClassDef | None:
    """Return the first class definition that has ``Scene`` in its base names.

    Args:
        tree: Parsed AST of the module.

    Returns:
        The matching ClassDef node, or None if not found.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = _extract_name(base)
                if base_name is not None and "Scene" in base_name:
                    return node
    return None


def _extract_name(node: ast.expr) -> str | None:
    """Extract a name string from a Name or Attribute AST node.

    Args:
        node: An expression node (typically a base class reference).

    Returns:
        The identifier string, or None if the node is neither Name nor Attribute.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None
