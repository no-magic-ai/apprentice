"""Legacy stage writers: explicit owned root, checked before any provider call."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from apprentice.core.artifacts import ArtifactError
from apprentice.models.work_item import PipelineContext, WorkItem
from apprentice.providers.base import Completion
from apprentice.stages.assessment import AssessmentStage
from apprentice.stages.discovery import DiscoveryStage
from apprentice.stages.implementation import ImplementationStage
from apprentice.stages.instrumentation import InstrumentationStage
from apprentice.stages.validation import ValidationStage
from apprentice.stages.visualization import VisualizationStage

if TYPE_CHECKING:
    from apprentice.core.session_store import SessionStore


class _CountingProvider:
    """Trusted fixture provider: returns fixed text and counts every request."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, options: dict[str, Any], max_tokens: int) -> Completion:
        self.calls += 1
        return Completion(
            text="```python\nfixture = 1\n```",
            input_tokens=1,
            output_tokens=1,
            model="fixture",
            stop_reason="stop",
        )


_STAGES: list[tuple[type[Any], str]] = [
    (ImplementationStage, "implementation.py"),
    (InstrumentationStage, "instrumented.py"),
    (VisualizationStage, "scene.py"),
    (AssessmentStage, "cards.csv"),
    (ValidationStage, "validation_report.json"),
    (DiscoveryStage, "discovery.json"),
]


def _context(provider: _CountingProvider, artifact_root: str, implementation: Path) -> Any:
    return PipelineContext(
        config={
            "provider": provider,
            "artifacts": {"implementation": str(implementation)},
            "implementation_artifact": str(implementation),
        },
        artifact_root=artifact_root,
    )


@pytest.fixture
def implementation(tmp_path: Path) -> Path:
    path = tmp_path / "reference_implementation.py"
    path.write_text('"""Selection sort."""\n\nx = 1\n', encoding="utf-8")
    return path


@pytest.mark.parametrize(("stage_cls", "filename"), _STAGES)
def test_stage_without_root_refuses_before_provider(
    stage_cls: type[Any], filename: str, implementation: Path
) -> None:
    provider = _CountingProvider()
    with pytest.raises(ArtifactError, match="no artifact root"):
        stage_cls().execute(
            WorkItem(id="t", algorithm_name="selection", tier=2),
            _context(provider, "", implementation),
        )
    assert provider.calls == 0


@pytest.mark.parametrize(("stage_cls", "filename"), _STAGES)
def test_stage_with_symlink_root_refuses_before_provider(
    stage_cls: type[Any], filename: str, implementation: Path, store: SessionStore, tmp_path: Path
) -> None:
    provider = _CountingProvider()
    target = store.allocate_work_root()
    link = tmp_path / "root-link"
    link.symlink_to(target)
    with pytest.raises(ArtifactError, match="symlink"):
        stage_cls().execute(
            WorkItem(id="t", algorithm_name="selection", tier=2),
            _context(provider, str(link), implementation),
        )
    assert provider.calls == 0
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(("stage_cls", "filename"), _STAGES)
def test_stage_writes_fixed_role_file_in_owned_root(
    stage_cls: type[Any], filename: str, implementation: Path, store: SessionStore
) -> None:
    root = store.allocate_work_root()
    result = stage_cls().execute(
        WorkItem(id="t", algorithm_name="selection", tier=2),
        _context(_CountingProvider(), str(root), implementation),
    )
    written = [Path(path) for path in result.artifacts.values()]
    assert written == [root / filename]
    assert [p.name for p in root.iterdir()] == [filename]
