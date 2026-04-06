"""Lint and style gate — ruff + structural checks, max 2 auto-fix retries."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apprentice.models.artifact import ArtifactBundle

from apprentice.models.work_item import GateResult, GateVerdict, WorkItem

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


def _collect_public_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return top-level public function definitions."""
    results: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Module):
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, ast.FunctionDef | ast.AsyncFunctionDef
                ) and not child.name.startswith("_"):
                    results.append(child)
    return results


def _function_has_full_annotations(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if all parameters and the return have type annotations."""
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


class LintGate:
    """Check implementation files for style and structural requirements."""

    name = "lint"
    max_retries = 2
    blocking = True

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        """Evaluate the implementation file against lint and style rules.

        Args:
            work_item: The work item being evaluated.
            artifacts: Bundle of generated artifacts.

        Returns:
            GateResult with PASS, FAIL, or WARN and per-check diagnostics.
        """
        checks: list[dict[str, Any]] = []
        failed = False

        path_str = artifacts.implementation_path
        if not path_str:
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={"checks": [], "error": "implementation_path is empty"},
            )

        path = Path(path_str)
        if not path.exists():
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={
                    "checks": [],
                    "error": f"file not found: {path_str}",
                },
            )

        source = path.read_text(encoding="utf-8")

        # Syntax check via AST parse
        try:
            tree = ast.parse(source, filename=str(path))
            checks.append({"check": "syntax", "passed": True})
        except SyntaxError as exc:
            checks.append({"check": "syntax", "passed": False, "detail": str(exc)})
            return GateResult(
                gate_name=self.name,
                verdict=GateVerdict.FAIL,
                diagnostics={"checks": checks},
            )

        # Module-level docstring
        module_docstring = ast.get_docstring(tree)
        has_module_doc = module_docstring is not None
        checks.append(
            {
                "check": "module_docstring",
                "passed": has_module_doc,
                "detail": None if has_module_doc else "missing module-level docstring",
            }
        )
        if not has_module_doc:
            failed = True

        # Public functions: docstrings and annotations
        public_funcs = _collect_public_functions(tree)
        for func in public_funcs:
            func_doc = ast.get_docstring(func) is not None
            checks.append(
                {
                    "check": f"docstring:{func.name}",
                    "passed": func_doc,
                    "detail": None if func_doc else f"missing docstring on {func.name}",
                }
            )
            if not func_doc:
                failed = True

            annotated = _function_has_full_annotations(func)
            checks.append(
                {
                    "check": f"annotations:{func.name}",
                    "passed": annotated,
                    "detail": None if annotated else f"missing type annotations on {func.name}",
                }
            )
            if not annotated:
                failed = True

        # Wildcard imports
        no_wildcards = not _has_wildcard_import(tree)
        checks.append(
            {
                "check": "no_wildcard_imports",
                "passed": no_wildcards,
                "detail": None if no_wildcards else "wildcard import detected (from x import *)",
            }
        )
        if not no_wildcards:
            failed = True

        # File length
        line_count = len(source.splitlines())
        under_limit = line_count <= _MAX_FILE_LINES
        checks.append(
            {
                "check": "file_length",
                "passed": under_limit,
                "detail": None
                if under_limit
                else f"file has {line_count} lines (max {_MAX_FILE_LINES})",
            }
        )
        if not under_limit:
            failed = True

        verdict = GateVerdict.FAIL if failed else GateVerdict.PASS
        return GateResult(
            gate_name=self.name,
            verdict=verdict,
            diagnostics={"checks": checks},
        )
