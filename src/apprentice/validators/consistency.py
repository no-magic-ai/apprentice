"""ConsistencyValidator — cross-artifact structural and semantic validation."""

from __future__ import annotations

import ast
import csv
import io
import re
from pathlib import Path
from typing import TYPE_CHECKING

from apprentice.validators.base import ValidationIssue, ValidationResult

if TYPE_CHECKING:
    from apprentice.models.work_item import WorkItem

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


class ConsistencyValidator:
    """Validate cross-artifact structural and semantic consistency."""

    name = "consistency"

    def validate(self, artifacts: dict[str, str], work_item: WorkItem) -> ValidationResult:
        """Check that all present artifacts are internally and mutually consistent.

        Args:
            artifacts: Mapping of artifact_type to file path.
            work_item: The work item being validated.

        Returns:
            ValidationResult with structural errors and semantic warnings.
        """
        issues: list[ValidationIssue] = []
        algorithm_name = work_item.algorithm_name

        impl_path_str = artifacts.get("implementation") or ""
        impl_source: str | None = None

        # --- Structural checks (severity="error") ---

        if impl_path_str:
            impl_path = Path(impl_path_str)
            if not impl_path.exists():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        message=f"implementation file not found: {impl_path_str}",
                        artifact="implementation",
                        suggestion="Generate missing implementation artifact",
                    )
                )
            else:
                try:
                    impl_source = impl_path.read_text(encoding="utf-8")
                    ast.parse(impl_source)
                except SyntaxError as exc:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            message=f"implementation contains invalid Python: {exc}",
                            artifact="implementation",
                            suggestion="Fix syntax errors in implementation",
                        )
                    )
                    impl_source = None

        instr_path_str = artifacts.get("instrumented") or ""
        if instr_path_str and not Path(instr_path_str).exists():
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"instrumented file not found: {instr_path_str}",
                    artifact="instrumented",
                    suggestion="Generate missing instrumented artifact",
                )
            )

        manim_path_str = artifacts.get("manim_scene") or ""
        if manim_path_str and not Path(manim_path_str).exists():
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"manim scene file not found: {manim_path_str}",
                    artifact="manim_scene",
                    suggestion="Generate missing manim_scene artifact",
                )
            )

        anki_path_str = artifacts.get("anki_deck") or ""
        anki_content: str | None = None
        if anki_path_str:
            anki_path = Path(anki_path_str)
            if not anki_path.exists():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        message=f"anki deck file not found: {anki_path_str}",
                        artifact="anki_deck",
                        suggestion="Generate missing anki_deck artifact",
                    )
                )
            else:
                anki_content = anki_path.read_text(encoding="utf-8")
                if not _anki_column_count_ok(anki_content):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            message=(
                                f"anki CSV rows must have exactly {_ANKI_REQUIRED_COLUMNS} columns"
                            ),
                            artifact="anki_deck",
                            suggestion=(
                                "Ensure CSV has exactly 4 columns: front, back, tags, type"
                            ),
                        )
                    )

        # --- Semantic checks (severity="warning") ---

        if impl_source is not None:
            impl_docstring = _extract_implementation_docstring(impl_source)
            if algorithm_name.lower() not in impl_docstring.lower():
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        message=(
                            f"algorithm name '{algorithm_name}' not found in implementation docstring"
                        ),
                        artifact="implementation",
                        suggestion=f"Include '{algorithm_name}' in the module docstring",
                    )
                )

            if anki_content is not None:
                impl_complexities = _extract_complexity_notation(impl_docstring)
                if impl_complexities:
                    anki_has_complexity = any(c in anki_content for c in impl_complexities)
                    if not anki_has_complexity:
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                message=(
                                    f"complexity {impl_complexities} from docstring not found in anki cards"
                                ),
                                artifact="anki_deck",
                                suggestion=(
                                    "Ensure complexity notation matches between implementation and Anki cards"
                                ),
                            )
                        )

        if manim_path_str and Path(manim_path_str).exists():
            manim_source = Path(manim_path_str).read_text(encoding="utf-8")
            name_slug = algorithm_name.lower().replace(" ", "").replace("-", "").replace("_", "")
            class_names_lower = "".join(
                c.lower() for c in re.findall(r"class\s+(\w+)", manim_source)
            )
            if name_slug not in class_names_lower:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        message=(
                            f"algorithm name '{algorithm_name}' not found in any manim scene class name"
                        ),
                        artifact="manim_scene",
                        suggestion=f"Include '{algorithm_name}' in the module docstring",
                    )
                )

        if anki_content is not None and algorithm_name.lower() not in anki_content.lower():
            issues.append(
                ValidationIssue(
                    severity="warning",
                    message=f"algorithm name '{algorithm_name}' not found in anki cards",
                    artifact="anki_deck",
                    suggestion=f"Include '{algorithm_name}' in the module docstring",
                )
            )

        has_errors = any(issue.severity == "error" for issue in issues)
        return ValidationResult(
            validator_name=self.name,
            passed=not has_errors,
            issues=issues,
        )
