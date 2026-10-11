"""Trusted staging keeps no descriptor open and leaves no partial file, whatever happens."""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING

import pytest

from apprentice.sandbox.errors import StagingError, UnsafeInputError
from apprentice.sandbox.staging import read_scoped, staged_copies, write_derived

if TYPE_CHECKING:
    from pathlib import Path


def _open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path


def _fail(*args: object, **kwargs: object) -> object:
    raise OSError(errno.EIO, "injected I/O error")


class TestDescriptors:
    def test_repeatedly_staging_a_directory_is_refused_without_leaking(self, root: Path) -> None:
        (root / "implementation.py").mkdir()
        before = _open_descriptors()

        for _ in range(400):
            with pytest.raises(UnsafeInputError):
                read_scoped(root, "implementation.py", limit=1 << 20)

        assert _open_descriptors() == before

    def test_a_failing_read_aborts_without_leaking(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (root / "implementation.py").write_text("x = 1\n")
        before = _open_descriptors()
        monkeypatch.setattr("apprentice.sandbox.staging.os.read", _fail)

        for _ in range(50):
            with pytest.raises(StagingError) as excinfo:
                read_scoped(root, "implementation.py", limit=1 << 20)
            assert not isinstance(excinfo.value, UnsafeInputError)

        monkeypatch.undo()
        assert _open_descriptors() == before

    def test_a_failing_private_copy_aborts_without_leaking(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (root / "implementation.py").write_text("x = 1\n")
        before = _open_descriptors()
        monkeypatch.setattr("apprentice.sandbox.staging.os.write", _fail)

        for _ in range(50):
            with (
                pytest.raises(StagingError, match="private copy"),
                staged_copies(root, {"implementation": "implementation.py"}),
            ):
                pass

        monkeypatch.undo()
        assert _open_descriptors() == before

    @pytest.mark.parametrize("failing", ["write", "fsync", "replace"])
    def test_a_failing_derived_write_leaves_nothing_behind(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, failing: str
    ) -> None:
        write_derived(root, "render", "scene.mp4", b"earlier")
        before = _open_descriptors()
        monkeypatch.setattr(f"apprentice.sandbox.staging.os.{failing}", _fail)

        for _ in range(20):
            with pytest.raises(StagingError, match="cannot place derived output"):
                write_derived(root, "render", "scene.mp4", b"new bytes")

        monkeypatch.undo()
        assert _open_descriptors() == before
        assert os.listdir(root / "render") == ["scene.mp4"]
        assert (root / "render" / "scene.mp4").read_bytes() == b"earlier"
