"""Validation script — compares generated implementation AST against reference.

Checks:
  - Structural similarity >85% (function count, signature patterns, docstring presence)
  - Token usage within 30% of estimate
  - Reports pass/fail with diagnostics
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any


def analyze_structure(code: str) -> dict[str, Any]:
    """Extract structural features from Python source code."""
    tree = ast.parse(code)

    functions: list[dict[str, Any]] = []
    classes: list[str] = []
    has_main_block = False
    has_module_docstring = (
        isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant)
        if tree.body
        else False
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            has_docstring = (
                bool(node.body)
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            )
            has_type_hints = node.returns is not None or any(
                arg.annotation is not None for arg in node.args.args
            )
            functions.append(
                {
                    "name": node.name,
                    "args": len(node.args.args),
                    "has_docstring": has_docstring,
                    "has_type_hints": has_type_hints,
                }
            )
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, ast.If):
            if (
                isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
            ):
                has_main_block = True

    return {
        "function_count": len(functions),
        "class_count": len(classes),
        "has_module_docstring": has_module_docstring,
        "has_main_block": has_main_block,
        "functions_with_docstrings": sum(1 for f in functions if f["has_docstring"]),
        "functions_with_type_hints": sum(1 for f in functions if f["has_type_hints"]),
        "functions": functions,
    }


def compute_similarity(generated: dict[str, Any], reference: dict[str, Any]) -> float:
    """Compute structural similarity score (0.0 to 1.0)."""
    scores: list[float] = []

    gen_funcs = int(generated["function_count"])
    ref_funcs = int(reference["function_count"])
    if ref_funcs > 0:
        scores.append(min(gen_funcs / ref_funcs, 1.0))
    elif gen_funcs > 0:
        scores.append(1.0)
    else:
        scores.append(0.5)

    scores.append(1.0 if generated["has_module_docstring"] else 0.0)
    scores.append(1.0 if generated["has_main_block"] else 0.0)

    gen_with_docs = int(generated["functions_with_docstrings"])
    if gen_funcs > 0:
        scores.append(gen_with_docs / gen_funcs)
    else:
        scores.append(0.0)

    gen_with_hints = int(generated["functions_with_type_hints"])
    if gen_funcs > 0:
        scores.append(gen_with_hints / gen_funcs)
    else:
        scores.append(0.0)

    return sum(scores) / len(scores) if scores else 0.0


def validate_token_usage(actual: int, estimated: int) -> tuple[bool, float]:
    """Check if actual token usage is within 30% of estimate."""
    if estimated == 0:
        return actual == 0, 0.0
    drift = abs(actual - estimated) / estimated
    return drift <= 0.30, drift


def validate_file(
    generated_path: Path,
    reference_path: Path | None = None,
    actual_tokens: int = 0,
    estimated_tokens: int = 0,
) -> dict[str, Any]:
    """Run full validation on a generated file."""
    gen_code = generated_path.read_text(encoding="utf-8")
    gen_struct = analyze_structure(gen_code)

    ref_struct: dict[str, Any] | None = None
    similarity = 0.0
    if reference_path and reference_path.exists():
        ref_code = reference_path.read_text(encoding="utf-8")
        ref_struct = analyze_structure(ref_code)
        similarity = compute_similarity(gen_struct, ref_struct)

    token_ok, token_drift = validate_token_usage(actual_tokens, estimated_tokens)

    structure_pass = similarity >= 0.85 if ref_struct else True
    token_pass = token_ok if estimated_tokens > 0 else True

    return {
        "file": str(generated_path),
        "structure": gen_struct,
        "reference_structure": ref_struct,
        "similarity": round(similarity, 4),
        "similarity_pass": structure_pass,
        "token_actual": actual_tokens,
        "token_estimated": estimated_tokens,
        "token_drift": round(token_drift, 4),
        "token_pass": token_pass,
        "overall_pass": structure_pass and token_pass,
    }


def main() -> int:
    """CLI entry: validate_foundation.py <generated_file> [--reference <ref_file>] [--tokens actual,estimated]."""
    if len(sys.argv) < 2:
        print("Usage: validate_foundation.py <generated_file> [--reference <ref>] [--tokens a,e]")
        return 1

    generated = Path(sys.argv[1])
    if not generated.exists():
        print(f"File not found: {generated}")
        return 1

    reference: Path | None = None
    actual_tokens = 0
    estimated_tokens = 0

    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--reference" and i + 1 < len(sys.argv):
            reference = Path(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == "--tokens" and i + 1 < len(sys.argv):
            parts = sys.argv[i + 1].split(",")
            actual_tokens = int(parts[0])
            estimated_tokens = int(parts[1])
            i += 2
        else:
            i += 1

    result = validate_file(generated, reference, actual_tokens, estimated_tokens)
    print(json.dumps(result, indent=2, default=str))

    return 0 if result["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
