"""LintValidator — structural and style checks adapted from LintGate."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from apprentice.validators.base import ValidationIssue, ValidationResult

if TYPE_CHECKING:
    from apprentice.models.work_item import WorkItem

_MAX_FILE_LINES = 500


def _has_wildcard_import(tree: ast.Module) -> bool:
    """Return True if the module contains any wildcard import."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.names
            and any(alias.name == "*" for alias in node.names)
        ):
            return True
    return False


def _wildcard_module_name(tree: ast.Module) -> str:
    """Return the first module name that has a wildcard import, or empty string."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.names
            and any(alias.name == "*" for alias in node.names)
        ):
            return node.module or ""
    return ""


def _collect_public_functions(
    tree: ast.Module,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return top-level public function definitions."""
    results: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and not child.name.startswith(
            "_"
        ):
            results.append(child)
    return results


def _function_has_full_annotations(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if all parameters and return have type annotations."""
    args = func.args
    all_params = (
        args.posonlyargs
        + args.args
        + args.kwonlyargs
        + ([args.vararg] if args.vararg else [])
        + ([args.kwarg] if args.kwarg else [])
    )
    for arg in all_params:
        if arg.arg == "self":
            continue
        if arg.annotation is None:
            return False
    return func.returns is not None


class LintValidator:
    """Check implementation files for style and structural requirements."""

    name = "lint"

    def validate(self, artifacts: dict[str, str], work_item: WorkItem) -> ValidationResult:
        """Validate the implementation file against lint and style rules.

        Args:
            artifacts: Mapping of artifact_type to file path.
            work_item: The work item being validated.

        Returns:
            ValidationResult with issues collected from all checks.
        """
        issues: list[ValidationIssue] = []

        path_str = artifacts.get("implementation") or ""
        if not path_str:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                issues=[
                    ValidationIssue(
                        severity="error",
                        message="implementation artifact path is empty",
                        artifact="implementation",
                        suggestion="Provide a non-empty path for the implementation artifact",
                    )
                ],
            )

        path = Path(path_str)
        if not path.exists():
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                issues=[
                    ValidationIssue(
                        severity="error",
                        message=f"implementation file not found: {path_str}",
                        artifact="implementation",
                        suggestion="Generate missing implementation artifact",
                    )
                ],
            )

        source = path.read_text(encoding="utf-8")

        # Syntax check
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                issues=[
                    ValidationIssue(
                        severity="error",
                        message=f"syntax error in implementation: {exc}",
                        artifact="implementation",
                        suggestion=f"Fix syntax error: {exc}",
                    )
                ],
            )

        # Module docstring
        if ast.get_docstring(tree) is None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message="missing module-level docstring",
                    artifact="implementation",
                    suggestion="Add a module-level docstring describing the algorithm",
                )
            )

        # Public functions: docstrings and annotations
        for func in _collect_public_functions(tree):
            if ast.get_docstring(func) is None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        message=f"missing docstring on function '{func.name}'",
                        artifact="implementation",
                        suggestion=f"Add a Google-style docstring to function '{func.name}'",
                    )
                )
            if not _function_has_full_annotations(func):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        message=f"missing type annotations on function '{func.name}'",
                        artifact="implementation",
                        suggestion=(
                            f"Add type annotations to all parameters and return type of '{func.name}'"
                        ),
                    )
                )

        # Wildcard imports
        if _has_wildcard_import(tree):
            module = _wildcard_module_name(tree)
            issues.append(
                ValidationIssue(
                    severity="error",
                    message="wildcard import detected",
                    artifact="implementation",
                    suggestion=f"Replace 'from {module} import *' with explicit imports",
                )
            )

        # File length
        line_count = len(source.splitlines())
        if line_count > _MAX_FILE_LINES:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"file has {line_count} lines (max {_MAX_FILE_LINES})",
                    artifact="implementation",
                    suggestion="Reduce file to under 500 lines by extracting helper functions",
                )
            )

        return ValidationResult(
            validator_name=self.name,
            passed=len(issues) == 0,
            issues=issues,
        )
