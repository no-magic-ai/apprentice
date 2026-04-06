"""Consistency gate — cross-artifact structural and semantic validation."""

from __future__ import annotations

import ast
import csv
import io
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.models.artifact import ArtifactBundle

from apprentice.models.work_item import GateResult, GateVerdict, WorkItem

_ANKI_REQUIRED_COLUMNS = 4
_COMPLEXITY_PATTERN = re.compile(r"O\([^)]+\)")


def _extract_implementation_docstring(source: str) -> str:
    """Return the module-level docstring from Python source, or empty string."""
    try:
        tree = ast.parse(source)
        return ast.get_docstring(tree) or ""
    except SyntaxError:
        return ""


def _extract_complexity_notation(text: str) -> list[str]:
    """Return all Big-O complexity tokens found in the text."""
    return _COMPLEXITY_PATTERN.findall(text)


def _anki_column_count_ok(csv_content: str) -> bool:
    """Return True if every non-empty row in the CSV has exactly 4 columns."""
    reader = csv.reader(io.StringIO(csv_content))
    for row in reader:
        if not row:
            continue
        if len(row) != _ANKI_REQUIRED_COLUMNS:
            return False
    return True


class ConsistencyGate:
    """Validate cross-artifact structural and semantic consistency."""

    name = "consistency"
    max_retries = 0
    blocking = True

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        """Check that all present artifacts are internally and mutually consistent.

        Args:
            work_item: The work item being evaluated.
            artifacts: Bundle of generated artifacts.

        Returns:
            GateResult with PASS, FAIL (structural), or WARN (semantic) plus per-check diagnostics.
        """
        checks: list[dict[str, Any]] = []
        structural_fail = False
        semantic_warn = False

        algorithm_name = work_item.algorithm_name

        # --- Structural checks ---

        # Implementation: file must exist and be valid Python
        impl_path_str = artifacts.implementation_path
        impl_source: str | None = None
        if impl_path_str:
            impl_path = Path(impl_path_str)
            if not impl_path.exists():
                checks.append(
                    {
                        "check": "implementation_exists",
                        "passed": False,
                        "detail": f"implementation file not found: {impl_path_str}",
                    }
                )
                structural_fail = True
            else:
                checks.append({"check": "implementation_exists", "passed": True})
                try:
                    impl_source = impl_path.read_text(encoding="utf-8")
                    ast.parse(impl_source)
                    checks.append({"check": "implementation_valid_python", "passed": True})
                except SyntaxError as exc:
                    checks.append(
                        {
                            "check": "implementation_valid_python",
                            "passed": False,
                            "detail": str(exc),
                        }
                    )
                    structural_fail = True

        # Instrumented: if path given, must exist
        instr_path_str = artifacts.instrumented_path
        if instr_path_str:
            instr_exists = Path(instr_path_str).exists()
            checks.append(
                {
                    "check": "instrumented_exists",
                    "passed": instr_exists,
                    "detail": None
                    if instr_exists
                    else f"instrumented file not found: {instr_path_str}",
                }
            )
            if not instr_exists:
                structural_fail = True

        # Manim: if path given, must exist
        manim_path_str = artifacts.manim_scene_path
        if manim_path_str:
            manim_exists = Path(manim_path_str).exists()
            checks.append(
                {
                    "check": "manim_exists",
                    "passed": manim_exists,
                    "detail": None if manim_exists else f"manim file not found: {manim_path_str}",
                }
            )
            if not manim_exists:
                structural_fail = True

        # Anki CSV: if path given, must exist and have correct column count
        anki_path_str = artifacts.anki_deck_path
        anki_content: str | None = None
        if anki_path_str:
            anki_path = Path(anki_path_str)
            if not anki_path.exists():
                checks.append(
                    {
                        "check": "anki_exists",
                        "passed": False,
                        "detail": f"anki file not found: {anki_path_str}",
                    }
                )
                structural_fail = True
            else:
                checks.append({"check": "anki_exists", "passed": True})
                anki_content = anki_path.read_text(encoding="utf-8")
                columns_ok = _anki_column_count_ok(anki_content)
                checks.append(
                    {
                        "check": "anki_column_count",
                        "passed": columns_ok,
                        "detail": None
                        if columns_ok
                        else f"anki CSV rows must have {_ANKI_REQUIRED_COLUMNS} columns",
                    }
                )
                if not columns_ok:
                    structural_fail = True

        # --- Semantic checks (advisory) ---

        # Algorithm name in implementation docstring
        if impl_source is not None:
            impl_docstring = _extract_implementation_docstring(impl_source)
            name_in_doc = algorithm_name.lower() in impl_docstring.lower()
            checks.append(
                {
                    "check": "name_in_implementation_docstring",
                    "passed": name_in_doc,
                    "detail": None
                    if name_in_doc
                    else (
                        f"algorithm name '{algorithm_name}' not found in implementation docstring"
                    ),
                }
            )
            if not name_in_doc:
                semantic_warn = True

            # Complexity consistency between implementation docstring and anki
            if anki_content is not None:
                impl_complexities = _extract_complexity_notation(impl_docstring)
                if impl_complexities:
                    anki_text = anki_content
                    anki_has_complexity = any(c in anki_text for c in impl_complexities)
                    checks.append(
                        {
                            "check": "complexity_consistency",
                            "passed": anki_has_complexity,
                            "detail": None
                            if anki_has_complexity
                            else (
                                f"complexity {impl_complexities} from docstring not found in anki cards"
                            ),
                        }
                    )
                    if not anki_has_complexity:
                        semantic_warn = True

        # Algorithm name in manim scene class name
        if manim_path_str and Path(manim_path_str).exists():
            manim_source = Path(manim_path_str).read_text(encoding="utf-8")
            name_slug = algorithm_name.lower().replace(" ", "").replace("-", "").replace("_", "")
            class_names_lower = "".join(
                c.lower() for c in re.findall(r"class\s+(\w+)", manim_source)
            )
            name_in_scene = name_slug in class_names_lower
            checks.append(
                {
                    "check": "name_in_manim_scene_class",
                    "passed": name_in_scene,
                    "detail": None
                    if name_in_scene
                    else (
                        f"algorithm name '{algorithm_name}' not found in any manim scene class name"
                    ),
                }
            )
            if not name_in_scene:
                semantic_warn = True

        # Algorithm name in anki content
        if anki_content is not None:
            name_in_anki = algorithm_name.lower() in anki_content.lower()
            checks.append(
                {
                    "check": "name_in_anki_cards",
                    "passed": name_in_anki,
                    "detail": None
                    if name_in_anki
                    else (f"algorithm name '{algorithm_name}' not found in anki cards"),
                }
            )
            if not name_in_anki:
                semantic_warn = True

        if structural_fail:
            verdict = GateVerdict.FAIL
        elif semantic_warn:
            verdict = GateVerdict.WARN
        else:
            verdict = GateVerdict.PASS

        return GateResult(
            gate_name=self.name,
            verdict=verdict,
            diagnostics={"checks": checks},
        )
