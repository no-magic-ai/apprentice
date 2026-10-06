"""Deterministic packaging against offline bare repositories (real git, recording gh)."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from apprentice.agents.packaging import PackagingError, submit_snapshot

if TYPE_CHECKING:
    from pathlib import Path

    from apprentice.core.artifacts import BundleSnapshot
    from apprentice.core.session_store import SessionStore
    from tests.conftest import OfflineRemotes

_CORE = "no-magic-ai/no-magic"
_VIZ = "no-magic-ai/no-magic-viz"
_APPROVAL = {"approved_by": "tester", "approved_at": "2026-10-06T12:00:00+00:00"}


def _snapshot(store: SessionStore, algorithm: str = "selection") -> BundleSnapshot:
    record = store.create_run(algorithm, 2)
    store.complete_run(
        record,
        {
            "generated_code": "impl = 1\n",
            "manim_scene_code": "scene = 1\n",
            "instrumented_code": "trace = 1\n",
            "anki_deck_content": "front,back\n",
        },
        {},
        1.0,
    )
    return store.load_bundle(record)


def test_promotes_exact_approved_bytes_to_each_destination(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)

    submissions = submit_snapshot(snapshot, _APPROVAL, store.allocate_work_root())

    branch = f"apprentice/{snapshot.run_id}"
    assert [s.repository for s in submissions] == [_CORE, _VIZ]
    by_role = {a.role: a.data for a in snapshot.artifacts}
    assert (
        offline_remotes.blob(_CORE, branch, "02-alignment/microselection.py")
        == by_role["implementation"]
    )
    assert (
        offline_remotes.blob(_VIZ, branch, "scenes/scene_microselection.py")
        == by_role["manim_scene"]
    )
    assert submissions[0].paths == ("02-alignment/microselection.py",)
    assert submissions[1].paths == ("scenes/scene_microselection.py",)


def test_commit_touches_only_approved_paths(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    submissions = submit_snapshot(snapshot, _APPROVAL, store.allocate_work_root())

    changed = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", submissions[0].commit],
        cwd=offline_remotes.bare[_CORE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert changed == ["02-alignment/microselection.py"]


def test_same_approval_produces_the_same_commit(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    first = submit_snapshot(snapshot, _APPROVAL, store.allocate_work_root())

    second = submit_snapshot(snapshot, _APPROVAL, store.allocate_work_root())

    assert [s.commit for s in second] == [s.commit for s in first]


def test_viz_pull_request_references_core_pull_request(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    submissions = submit_snapshot(snapshot, _APPROVAL, store.allocate_work_root())

    calls = offline_remotes.gh_calls()
    assert [call[call.index("--repo") + 1] for call in calls] == [_CORE, _VIZ]
    viz_body = calls[1][calls[1].index("--body") + 1]
    assert f"Companion PR: {submissions[0].pr_url}" in viz_body
    assert snapshot.manifest_sha256 in viz_body


def test_existing_destination_fails_before_any_push(
    store: SessionStore, offline_remotes: OfflineRemotes, tmp_path: Path
) -> None:
    seed = tmp_path / "collide"
    subprocess.run(["git", "clone", "-q", str(offline_remotes.bare[_VIZ]), str(seed)], check=True)
    (seed / "scenes" / "scene_microselection.py").write_text("upstream = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "upstream"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)

    with pytest.raises(PackagingError, match="already exists"):
        submit_snapshot(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []


def test_symlinked_destination_parent_fails_before_any_push(
    store: SessionStore, offline_remotes: OfflineRemotes, tmp_path: Path
) -> None:
    seed = tmp_path / "link"
    subprocess.run(["git", "clone", "-q", str(offline_remotes.bare[_CORE]), str(seed)], check=True)
    subprocess.run(["git", "rm", "-q", "-r", "02-alignment"], cwd=seed, check=True)
    os.symlink("/tmp", seed / "02-alignment")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "link"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)

    with pytest.raises(PackagingError, match="not a plain directory"):
        submit_snapshot(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.gh_calls() == []


def _effects(excinfo: pytest.ExceptionInfo[PackagingError]) -> list[tuple[str, bool, bool]]:
    return [(e["repository"], e["pushed"], bool(e["pr_url"])) for e in excinfo.value.effects]


def test_failure_before_any_push_reports_no_effects(
    store: SessionStore, offline_remotes: OfflineRemotes, tmp_path: Path
) -> None:
    seed = tmp_path / "collide-core"
    subprocess.run(["git", "clone", "-q", str(offline_remotes.bare[_CORE]), str(seed)], check=True)
    (seed / "02-alignment" / "microselection.py").write_text("upstream = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "upstream"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)

    with pytest.raises(PackagingError) as excinfo:
        submit_snapshot(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert excinfo.value.effects == []


def test_second_push_failure_reports_the_pushed_core_branch(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    offline_remotes.reject_pushes(_VIZ)
    snapshot = _snapshot(store)

    with pytest.raises(PackagingError, match="push rejected") as excinfo:
        submit_snapshot(snapshot, _APPROVAL, store.allocate_work_root())

    assert _effects(excinfo) == [(_CORE, True, False), (_VIZ, False, False)]
    assert f"apprentice/{snapshot.run_id}" in offline_remotes.branches(_CORE)
    assert offline_remotes.gh_calls() == []


def test_second_pull_request_failure_reports_the_opened_core_pr(
    store: SessionStore, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFFLINE_GH_FAIL_ON", "2")

    with pytest.raises(PackagingError, match="configured failure") as excinfo:
        submit_snapshot(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert _effects(excinfo) == [(_CORE, True, True), (_VIZ, True, False)]
    assert excinfo.value.effects[0]["pr_url"].endswith("/pull/offline-1")


def test_packaging_uses_captured_bytes_even_if_the_bundle_changes_after_verification(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    snapshot = _snapshot(store)
    approved = {a.role: a.data for a in snapshot.artifacts}
    bundle = store.bundle_dir(snapshot.run_id)
    for name in ("implementation.py", "scene.py"):
        path = bundle / name
        path.chmod(0o644)
        path.write_bytes(b"swapped_after_verification = True\n")

    submit_snapshot(snapshot, _APPROVAL, store.allocate_work_root())

    branch = f"apprentice/{snapshot.run_id}"
    assert (
        offline_remotes.blob(_CORE, branch, "02-alignment/microselection.py")
        == approved["implementation"]
    )
    assert (
        offline_remotes.blob(_VIZ, branch, "scenes/scene_microselection.py")
        == approved["manim_scene"]
    )


def test_commit_whose_stored_blob_differs_is_never_pushed(
    store: SessionStore, offline_remotes: OfflineRemotes
) -> None:
    offline_remotes.mutate_staged_python(_CORE)

    with pytest.raises(PackagingError, match="differs from approval") as excinfo:
        submit_snapshot(_snapshot(store), _APPROVAL, store.allocate_work_root())

    assert excinfo.value.effects == []
    assert offline_remotes.branches(_CORE) == ["main"]
    assert offline_remotes.branches(_VIZ) == ["main"]
    assert offline_remotes.gh_calls() == []
