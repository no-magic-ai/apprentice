"""Validator tools bound to one trusted artifact root — what a caller or agent may invoke.

The root (a run's work root) is fixed by the trusted caller when the tools are
created; tool arguments only ever name files inside it. Every file is staged
from the root without following links before a validator reads it. A path that
names no stageable file — outside the root (another run, a credential file),
a symlink or hard link, a directory, FIFO or other special file, or a file
over the size limit — gets the tool's ordinary failed result and is never
read or run. A fault of the root or of reading it (`StagingError`) is not
such a refusal: it propagates and aborts. Correctness runs the staged bytes in
the execution sandbox; there is no host execution and no sandbox-less fallback.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apprentice.models.work_item import WorkItem
from apprentice.sandbox.errors import SandboxUnavailableError, UnsafeInputError
from apprentice.sandbox.staging import MAX_ARTIFACT_BYTES, read_scoped, staged_copies
from apprentice.validators.base import ValidationIssue, ValidationResult
from apprentice.validators.consistency import ConsistencyValidator
from apprentice.validators.correctness import CorrectnessValidator
from apprentice.validators.lint import LintValidator
from apprentice.validators.schema_compliance import SchemaComplianceValidator

if TYPE_CHECKING:
    from apprentice.sandbox.docker import Sandbox


def _refused(validator_name: str, artifact: str, problem: str) -> dict[str, Any]:
    return ValidationResult(
        validator_name=validator_name,
        passed=False,
        issues=[
            ValidationIssue(
                severity="error",
                message=problem,
                artifact=artifact,
                suggestion="Name a regular file inside the run's artifact root",
            )
        ],
    ).to_dict()


def _artifact_paths(artifacts_json: str) -> dict[str, str] | None:
    try:
        artifacts = json.loads(artifacts_json)
    except ValueError:
        return None
    if not isinstance(artifacts, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in artifacts.items()
    ):
        return None
    return artifacts


class ValidatorTools:
    """The validator tools of one trusted artifact root (and, for correctness, one sandbox)."""

    def __init__(self, root: Path, sandbox: Sandbox | None = None) -> None:
        self.root = root
        self._sandbox = sandbox

    def lint_validate(self, code_path: str) -> dict[str, Any]:
        """Run lint validation on a Python implementation file in the artifact root.

        Checks syntax, module docstring, public function docstrings,
        type annotations, wildcard imports, and file length.

        Args:
            code_path: Path of the Python file inside the artifact root.

        Returns:
            Validation result with passed status and list of issues.
        """
        try:
            with staged_copies(self.root, {"implementation": code_path}) as staged:
                work_item = WorkItem(id="tool-call", algorithm_name=Path(code_path).stem, tier=0)
                return LintValidator().validate(staged, work_item).to_dict()
        except UnsafeInputError as exc:
            return _refused("lint", "implementation", str(exc))

    def correctness_validate(self, code_path: str) -> dict[str, Any]:
        """Run correctness validation by executing a Python file in the sandbox.

        Checks for a __main__ block and runs the file under the sandbox's
        time, output, memory and process limits. Reports runtime errors and
        assertion failures.

        Args:
            code_path: Path of the Python file inside the artifact root.

        Returns:
            Validation result with passed status and list of issues.

        Raises:
            SandboxUnavailableError: If these tools have no sandbox.
            SandboxInfrastructureError: If the program could not be run safely.
        """
        if self._sandbox is None:
            raise SandboxUnavailableError("these validator tools have no execution sandbox")
        work_item = WorkItem(id="tool-call", algorithm_name=Path(code_path).stem, tier=0)
        validator = CorrectnessValidator(self._sandbox, self.root)
        try:
            return validator.validate({"implementation": code_path}, work_item).to_dict()
        except UnsafeInputError as exc:
            return _refused("correctness", "implementation", str(exc))

    def consistency_validate(self, artifacts_json: str, algorithm_name: str) -> dict[str, Any]:
        """Run cross-artifact consistency validation.

        Checks structural integrity (files exist, valid Python, CSV columns)
        and semantic consistency (algorithm name in docstrings, complexity notation matches).

        Args:
            artifacts_json: JSON object mapping artifact types to file paths inside
                the artifact root, e.g. '{"implementation": "implementation.py"}'.
            algorithm_name: Algorithm the artifacts must consistently describe.

        Returns:
            Validation result with passed status and list of issues.
        """
        artifacts = _artifact_paths(artifacts_json)
        if artifacts is None:
            return _refused(
                "consistency", "artifacts", "artifacts_json is not a JSON object of paths"
            )
        try:
            with staged_copies(self.root, artifacts) as staged:
                work_item = WorkItem(id="tool-call", algorithm_name=algorithm_name, tier=0)
                return ConsistencyValidator().validate(staged, work_item).to_dict()
        except UnsafeInputError as exc:
            return _refused("consistency", "artifacts", str(exc))

    def schema_validate(self, artifacts_json: str) -> dict[str, Any]:
        """Run schema compliance validation against no-magic conventions.

        Checks implementation docstring sections, instrumented trace keys,
        Anki card types, and manim Scene subclass presence.

        Args:
            artifacts_json: JSON object mapping artifact types to file paths inside
                the artifact root, e.g. '{"implementation": "implementation.py"}'.

        Returns:
            Validation result with passed status and list of issues.
        """
        artifacts = _artifact_paths(artifacts_json)
        if artifacts is None:
            return _refused(
                "schema_compliance", "artifacts", "artifacts_json is not a JSON object of paths"
            )
        try:
            with staged_copies(self.root, artifacts) as staged:
                algorithm_name = Path(artifacts.get("implementation", "unknown")).stem
                work_item = WorkItem(id="tool-call", algorithm_name=algorithm_name, tier=0)
                return SchemaComplianceValidator().validate(staged, work_item).to_dict()
        except UnsafeInputError as exc:
            return _refused("schema_compliance", "artifacts", str(exc))

    def stdlib_check(self, code_path: str) -> dict[str, Any]:
        """Check that a Python file in the artifact root uses only standard library imports.

        Parses the file with AST and identifies any non-stdlib imports.

        Args:
            code_path: Path of the Python file inside the artifact root.

        Returns:
            Dict with 'passed' bool and 'violations' list of non-stdlib module names.
        """
        try:
            data = read_scoped(self.root, code_path, limit=MAX_ARTIFACT_BYTES)
        except UnsafeInputError as exc:
            return {"passed": False, "violations": [], "error": str(exc)}
        if data is None:
            return {"passed": False, "violations": [], "error": f"File not found: {code_path}"}
        try:
            tree = ast.parse(data.decode("utf-8"), filename=code_path)
        except (SyntaxError, UnicodeDecodeError) as exc:
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
