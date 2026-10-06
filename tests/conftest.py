"""Shared fixtures: real store-owned run scopes for agent and pipeline tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from apprentice.core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path

    from apprentice.core.artifacts import RunScope


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(store_dir=tmp_path / "sessions")


@pytest.fixture
def scope(store: SessionStore) -> RunScope:
    return store.run_scope(store.create_run("selection", 2))
