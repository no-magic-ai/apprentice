"""Schema compliance gate — validates artifacts against no-magic-schema.yaml."""

from __future__ import annotations

import ast
import csv
import io
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from apprentice.models.work_item import GateResult, GateVerdict, WorkItem

if TYPE_CHECKING:
    from apprentice.models.artifact import ArtifactBundle

_SCHEMA_PATH = Path(__file__).parents[3] / "config" / "no-magic-schema.yaml"

# Docstring section headers expected per the schema (Google-style lowercase)
_DOCSTRING_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "summary": re.compile(r"\S"),  # any non-blank content at the top counts
    "args": re.compile(r"^\s*Args\s*:", re.MULTILINE),
    "returns": re.compile(r"^\s*Returns\s*:", re.MULTILINE),
    "complexity": re.compile(r"complexity", re.IGNORECASE),
    "references": re.compile(r"^\s*References?\s*:", re.MULTILINE),
}

# Pattern for trace dict keys in instrumented source
_TRACE_KEY_PATTERN = re.compile(r"""["'](\w+)["']\s*:""")


def _load_schema(schema_path: Path) -> dict[str, Any]:
    """Load and return the YAML schema as a dict."""
    with schema_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise TypeError(f"schema at {schema_path} is not a YAML mapping")
    return data


def _check_implementation(source: str, required_sections: list[str]) -> list[dict[str, Any]]:
    """Validate the module-level docstring has all required sections."""
    checks: list[dict[str, Any]] = []
    try:
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree) or ""
    except SyntaxError as exc:
        return [{"check": "implementation_syntax", "passed": False, "detail": str(exc)}]

    checks.append({"check": "implementation_syntax", "passed": True})

    for section in required_sections:
        pattern = _DOCSTRING_SECTION_PATTERNS.get(section)
        if pattern is None:
            # Fallback: look for the section name as a keyword
            pattern = re.compile(re.escape(section), re.IGNORECASE)

        found: bool = (
            bool(docstring.strip()) if section == "summary" else bool(pattern.search(docstring))
        )

        checks.append(
            {
                "check": f"implementation_section:{section}",
                "passed": found,
                "detail": None if found else f"missing docstring section: {section}",
            }
        )
    return checks


def _check_instrumented(source: str, required_keys: list[str]) -> list[dict[str, Any]]:
    """Check that the instrumented source references all required trace keys."""
    checks: list[dict[str, Any]] = []
    found_keys = set(_TRACE_KEY_PATTERN.findall(source))
    for key in required_keys:
        present = key in found_keys
        checks.append(
            {
                "check": f"instrumented_trace_key:{key}",
                "passed": present,
                "detail": None if present else f"trace key '{key}' not found in instrumented file",
            }
        )
    return checks


def _check_anki(csv_content: str, required_types: list[str]) -> list[dict[str, Any]]:
    """Check that the CSV contains at least one card of each required type."""
    checks: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(csv_content))
    rows = [row for row in reader if row]

    # The schema requires front, back, tags, type — type is column index 3
    found_types: set[str] = set()
    for row in rows:
        if len(row) >= 4:
            found_types.add(row[3].strip().lower())

    for card_type in required_types:
        present = card_type.lower() in found_types
        checks.append(
            {
                "check": f"anki_card_type:{card_type}",
                "passed": present,
                "detail": None if present else f"no anki card of type '{card_type}' found",
            }
        )
    return checks


def _check_manim(source: str) -> list[dict[str, Any]]:
    """Check that the manim source defines at least one Scene subclass."""
    checks: list[dict[str, Any]] = []
    scene_class_pattern = re.compile(r"class\s+\w+\s*\([^)]*Scene[^)]*\)")
    has_scene = bool(scene_class_pattern.search(source))
    checks.append(
        {
            "check": "manim_scene_subclass",
            "passed": has_scene,
            "detail": None if has_scene else "no Scene subclass found in manim file",
        }
    )
    return checks


class SchemaComplianceGate:
    """Validate all present artifacts conform to no-magic-schema.yaml conventions."""

    name = "schema_compliance"
    max_retries = 0
    blocking = True

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        """Check each present artifact against the convention schema.

        Args:
            work_item: The work item being evaluated.
            artifacts: Bundle of generated artifacts.

        Returns:
            GateResult with PASS or FAIL and per-artifact diagnostics.
        """
        schema = _load_schema(_SCHEMA_PATH)
        algo_struct: dict[str, Any] = schema.get("algorithm_structure", {})
        required_sections: list[str] = algo_struct.get("required_docstring_sections", [])

        instrumentation: dict[str, Any] = schema.get("instrumentation", {})
        required_trace_keys: list[str] = instrumentation.get("required_keys", [])

        anki_schema: dict[str, Any] = schema.get("anki", {})
        required_card_types: list[str] = anki_schema.get("card_types", [])

        all_checks: list[dict[str, Any]] = []
        failed = False

        # Implementation
        impl_path_str = artifacts.implementation_path
        if impl_path_str:
            impl_path = Path(impl_path_str)
            if not impl_path.exists():
                all_checks.append(
                    {
                        "check": "implementation_file_exists",
                        "passed": False,
                        "detail": f"not found: {impl_path_str}",
                    }
                )
                failed = True
            else:
                impl_source = impl_path.read_text(encoding="utf-8")
                impl_checks = _check_implementation(impl_source, required_sections)
                all_checks.extend(impl_checks)
                if any(not c["passed"] for c in impl_checks):
                    failed = True

        # Instrumented
        instr_path_str = artifacts.instrumented_path
        if instr_path_str:
            instr_path = Path(instr_path_str)
            if not instr_path.exists():
                all_checks.append(
                    {
                        "check": "instrumented_file_exists",
                        "passed": False,
                        "detail": f"not found: {instr_path_str}",
                    }
                )
                failed = True
            else:
                instr_source = instr_path.read_text(encoding="utf-8")
                instr_checks = _check_instrumented(instr_source, required_trace_keys)
                all_checks.extend(instr_checks)
                if any(not c["passed"] for c in instr_checks):
                    failed = True

        # Anki CSV
        anki_path_str = artifacts.anki_deck_path
        if anki_path_str:
            anki_path = Path(anki_path_str)
            if not anki_path.exists():
                all_checks.append(
                    {
                        "check": "anki_file_exists",
                        "passed": False,
                        "detail": f"not found: {anki_path_str}",
                    }
                )
                failed = True
            else:
                anki_content = anki_path.read_text(encoding="utf-8")
                anki_checks = _check_anki(anki_content, required_card_types)
                all_checks.extend(anki_checks)
                if any(not c["passed"] for c in anki_checks):
                    failed = True

        # Manim scene
        manim_path_str = artifacts.manim_scene_path
        if manim_path_str:
            manim_path = Path(manim_path_str)
            if not manim_path.exists():
                all_checks.append(
                    {
                        "check": "manim_file_exists",
                        "passed": False,
                        "detail": f"not found: {manim_path_str}",
                    }
                )
                failed = True
            else:
                manim_source = manim_path.read_text(encoding="utf-8")
                manim_checks = _check_manim(manim_source)
                all_checks.extend(manim_checks)
                if any(not c["passed"] for c in manim_checks):
                    failed = True

        verdict = GateVerdict.FAIL if failed else GateVerdict.PASS
        return GateResult(
            gate_name=self.name,
            verdict=verdict,
            diagnostics={"checks": all_checks},
        )
