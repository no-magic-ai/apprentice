"""SchemaComplianceValidator — validates artifacts against no-magic-schema.yaml."""

from __future__ import annotations

import ast
import csv
import io
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from apprentice.validators.base import ValidationIssue, ValidationResult

if TYPE_CHECKING:
    from apprentice.models.work_item import WorkItem

_SCHEMA_PATH = Path(__file__).parents[3] / "config" / "no-magic-schema.yaml"

_DOCSTRING_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "summary": re.compile(r"\S"),
    "args": re.compile(r"^\s*Args\s*:", re.MULTILINE),
    "returns": re.compile(r"^\s*Returns\s*:", re.MULTILINE),
    "complexity": re.compile(r"complexity", re.IGNORECASE),
    "references": re.compile(r"^\s*References?\s*:", re.MULTILINE),
}

_TRACE_KEY_PATTERN = re.compile(r"""["'](\w+)["']\s*:""")


def _load_schema(schema_path: Path) -> dict[str, Any]:
    """Load and return the YAML schema as a dict."""
    with schema_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise TypeError(f"schema at {schema_path} is not a YAML mapping")
    return data


def _check_implementation_sections(
    source: str,
    required_sections: list[str],
) -> list[ValidationIssue]:
    """Validate the module-level docstring has all required sections."""
    issues: list[ValidationIssue] = []
    try:
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree) or ""
    except SyntaxError as exc:
        return [
            ValidationIssue(
                severity="error",
                message=f"implementation syntax error: {exc}",
                artifact="implementation",
                suggestion=f"Fix syntax error: {exc}",
            )
        ]

    for section in required_sections:
        pattern = _DOCSTRING_SECTION_PATTERNS.get(section)
        if pattern is None:
            pattern = re.compile(re.escape(section), re.IGNORECASE)

        found: bool = (
            bool(docstring.strip()) if section == "summary" else bool(pattern.search(docstring))
        )

        if not found:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"missing docstring section: {section}",
                    artifact="implementation",
                    suggestion=f"Add '{section}' section to the module docstring",
                )
            )

    return issues


def _check_instrumented_keys(
    source: str,
    required_keys: list[str],
) -> list[ValidationIssue]:
    """Check that the instrumented source references all required trace keys."""
    issues: list[ValidationIssue] = []
    found_keys = set(_TRACE_KEY_PATTERN.findall(source))
    missing = [k for k in required_keys if k not in found_keys]
    if missing:
        issues.append(
            ValidationIssue(
                severity="error",
                message=f"trace keys missing from instrumented file: {missing}",
                artifact="instrumented",
                suggestion=f"Ensure trace dictionaries include keys: {', '.join(missing)}",
            )
        )
    return issues


def _check_anki_card_types(
    csv_content: str,
    required_types: list[str],
) -> list[ValidationIssue]:
    """Check that the CSV contains at least one card of each required type."""
    issues: list[ValidationIssue] = []
    reader = csv.reader(io.StringIO(csv_content))
    found_types: set[str] = set()
    for row in reader:
        if row and len(row) >= 4:
            found_types.add(row[3].strip().lower())

    for card_type in required_types:
        if card_type.lower() not in found_types:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"no anki card of type '{card_type}' found",
                    artifact="anki_deck",
                    suggestion=f"Add cards of type '{card_type}' to the Anki deck",
                )
            )

    return issues


def _check_manim_scene_subclass(source: str) -> list[ValidationIssue]:
    """Check that the manim source defines at least one Scene subclass."""
    scene_class_pattern = re.compile(r"class\s+\w+\s*\([^)]*Scene[^)]*\)")
    if not scene_class_pattern.search(source):
        return [
            ValidationIssue(
                severity="error",
                message="no Scene subclass found in manim file",
                artifact="manim_scene",
                suggestion=("Define a class inheriting from manim.Scene with a construct() method"),
            )
        ]
    return []


class SchemaComplianceValidator:
    """Validate all present artifacts conform to no-magic-schema.yaml conventions."""

    name = "schema_compliance"

    def validate(self, artifacts: dict[str, str], work_item: WorkItem) -> ValidationResult:
        """Check each present artifact against the convention schema.

        Args:
            artifacts: Mapping of artifact_type to file path.
            work_item: The work item being validated.

        Returns:
            ValidationResult with issues for any schema violations.
        """
        schema = _load_schema(_SCHEMA_PATH)
        algo_struct: dict[str, Any] = schema.get("algorithm_structure", {})
        required_sections: list[str] = algo_struct.get("required_docstring_sections", [])

        instrumentation: dict[str, Any] = schema.get("instrumentation", {})
        required_trace_keys: list[str] = instrumentation.get("required_keys", [])

        anki_schema: dict[str, Any] = schema.get("anki", {})
        required_card_types: list[str] = anki_schema.get("card_types", [])

        issues: list[ValidationIssue] = []

        # Implementation
        impl_path_str = artifacts.get("implementation") or ""
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
                impl_source = impl_path.read_text(encoding="utf-8")
                issues.extend(_check_implementation_sections(impl_source, required_sections))

        # Instrumented
        instr_path_str = artifacts.get("instrumented") or ""
        if instr_path_str:
            instr_path = Path(instr_path_str)
            if not instr_path.exists():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        message=f"instrumented file not found: {instr_path_str}",
                        artifact="instrumented",
                        suggestion="Generate missing instrumented artifact",
                    )
                )
            else:
                instr_source = instr_path.read_text(encoding="utf-8")
                issues.extend(_check_instrumented_keys(instr_source, required_trace_keys))

        # Anki CSV
        anki_path_str = artifacts.get("anki_deck") or ""
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
                issues.extend(_check_anki_card_types(anki_content, required_card_types))

        # Manim scene
        manim_path_str = artifacts.get("manim_scene") or ""
        if manim_path_str:
            manim_path = Path(manim_path_str)
            if not manim_path.exists():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        message=f"manim scene file not found: {manim_path_str}",
                        artifact="manim_scene",
                        suggestion="Generate missing manim_scene artifact",
                    )
                )
            else:
                manim_source = manim_path.read_text(encoding="utf-8")
                issues.extend(_check_manim_scene_subclass(manim_source))

        return ValidationResult(
            validator_name=self.name,
            passed=len(issues) == 0,
            issues=issues,
        )
