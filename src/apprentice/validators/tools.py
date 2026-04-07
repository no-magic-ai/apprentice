"""FunctionTool wrappers for validators — ADK agents call these as tools."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

from apprentice.validators.consistency import ConsistencyValidator
from apprentice.validators.correctness import CorrectnessValidator
from apprentice.validators.lint import LintValidator
from apprentice.validators.schema_compliance import SchemaComplianceValidator


def lint_validate(code_path: str) -> dict[str, Any]:
    """Run lint validation on a Python implementation file.

    Checks syntax, module docstring, public function docstrings,
    type annotations, wildcard imports, and file length.

    Args:
        code_path: Absolute path to the Python file to validate.

    Returns:
        Validation result with passed status and list of issues.
    """
    from apprentice.models.work_item import WorkItem

    work_item = WorkItem(id="tool-call", algorithm_name=Path(code_path).stem, tier=0)
    validator = LintValidator()
    result = validator.validate({"implementation": code_path}, work_item)
    return result.to_dict()


def correctness_validate(code_path: str) -> dict[str, Any]:
    """Run correctness validation by executing a Python file.

    Checks for a __main__ block and executes the file with a 5-second timeout.
    Reports runtime errors and assertion failures.

    Args:
        code_path: Absolute path to the Python file to validate.

    Returns:
        Validation result with passed status and list of issues.
    """
    from apprentice.models.work_item import WorkItem

    work_item = WorkItem(id="tool-call", algorithm_name=Path(code_path).stem, tier=0)
    validator = CorrectnessValidator()
    result = validator.validate({"implementation": code_path}, work_item)
    return result.to_dict()


def consistency_validate(artifacts_json: str) -> dict[str, Any]:
    """Run cross-artifact consistency validation.

    Checks structural integrity (files exist, valid Python, CSV columns)
    and semantic consistency (algorithm name in docstrings, complexity notation matches).

    Args:
        artifacts_json: JSON string mapping artifact types to file paths,
            e.g. '{"implementation": "/path/to/algo.py", "anki_deck": "/path/to/cards.csv"}'.

    Returns:
        Validation result with passed status and list of issues.
    """
    from apprentice.models.work_item import WorkItem

    artifacts: dict[str, str] = json.loads(artifacts_json)
    algorithm_name = Path(artifacts.get("implementation", "unknown")).stem
    work_item = WorkItem(id="tool-call", algorithm_name=algorithm_name, tier=0)
    validator = ConsistencyValidator()
    result = validator.validate(artifacts, work_item)
    return result.to_dict()


def schema_validate(artifacts_json: str) -> dict[str, Any]:
    """Run schema compliance validation against no-magic conventions.

    Checks implementation docstring sections, instrumented trace keys,
    Anki card types, and manim Scene subclass presence.

    Args:
        artifacts_json: JSON string mapping artifact types to file paths,
            e.g. '{"implementation": "/path/to/algo.py"}'.

    Returns:
        Validation result with passed status and list of issues.
    """
    from apprentice.models.work_item import WorkItem

    artifacts: dict[str, str] = json.loads(artifacts_json)
    algorithm_name = Path(artifacts.get("implementation", "unknown")).stem
    work_item = WorkItem(id="tool-call", algorithm_name=algorithm_name, tier=0)
    validator = SchemaComplianceValidator()
    result = validator.validate(artifacts, work_item)
    return result.to_dict()


def stdlib_check(code_path: str) -> dict[str, Any]:
    """Check that a Python file uses only standard library imports.

    Parses the file with AST and identifies any non-stdlib top-level imports.

    Args:
        code_path: Absolute path to the Python file to check.

    Returns:
        Dict with 'passed' bool and 'violations' list of non-stdlib module names.
    """
    path = Path(code_path)
    if not path.exists():
        return {"passed": False, "violations": [], "error": f"File not found: {code_path}"}

    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=code_path)
    except SyntaxError as exc:
        return {"passed": False, "violations": [], "error": f"Syntax error: {exc}"}

    stdlib: frozenset[str] = frozenset(sys.stdlib_module_names)
    violations: list[str] = []
    seen: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in stdlib and root not in seen:
                    seen.add(root)
                    violations.append(root)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root and root not in stdlib and root not in seen:
                seen.add(root)
                violations.append(root)

    return {"passed": len(violations) == 0, "violations": violations}
