"""ValidatorInterface protocol — contract for all artifact validators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from apprentice.models.work_item import WorkItem


@dataclass
class ValidationIssue:
    """A single problem found during artifact validation."""

    severity: str  # "error", "warning", "info"
    message: str
    artifact: str  # Which artifact has the issue
    suggestion: str  # Actionable fix hint

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "message": self.message,
            "artifact": self.artifact,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            severity=data["severity"],
            message=data["message"],
            artifact=data["artifact"],
            suggestion=data.get("suggestion", ""),
        )


@dataclass
class ValidationResult:
    """Outcome of a single validator run against a set of artifacts."""

    validator_name: str
    passed: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "validator_name": self.validator_name,
            "passed": self.passed,
            "issues": [issue.to_dict() for issue in self.issues],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            validator_name=data["validator_name"],
            passed=data["passed"],
            issues=[ValidationIssue.from_dict(i) for i in data.get("issues", [])],
        )


@runtime_checkable
class ValidatorInterface(Protocol):
    """Contract for all artifact validators.

    Validators inspect a set of artifacts against work item requirements and
    return a structured result with per-issue diagnostics.
    """

    name: str

    def validate(
        self,
        artifacts: dict[str, str],  # artifact_type -> file_path
        work_item: WorkItem,
    ) -> ValidationResult:
        """Validate artifacts against work item requirements."""
        ...
