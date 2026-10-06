"""Consumer boundaries of run-owned artifacts: roots, writers and sealed-bundle verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from apprentice.agents.implementation import _make_after_drafter_callback
from apprentice.agents.review import build_review_agent
from apprentice.core.artifacts import (
    MANIFEST_FILENAME,
    ArtifactError,
    canonical_json,
    load_snapshot,
    manifest_digest,
    require_owned_root,
    seal_bundle,
    write_role,
)
from tests.conftest import OfflineFixtureLlm, fixture_outputs

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from apprentice.core.artifacts import RunScope
    from apprentice.core.session_store import SessionStore


def _sealed(store: SessionStore) -> Path:
    record = store.create_run("selection", 2)
    store.complete_run(
        record,
        {
            "generated_code": "impl = 1\n",
            "manim_scene_code": "scene = 1\n",
            "anki_deck_content": "front,back\n",
        },
        {},
        1.0,
    )
    return store.bundle_dir(record.run_id)


def _writable(path: Path) -> Path:
    path.chmod(0o644)
    return path


def _rewrite_manifest(bundle: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Edit the manifest body and re-sign it with a correct digest."""
    path = _writable(bundle / MANIFEST_FILENAME)
    manifest = json.loads(path.read_bytes())
    del manifest["manifest_sha256"]
    mutate(manifest)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    path.write_bytes(canonical_json(manifest))


def _entry(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    return next(e for e in manifest["artifacts"] if e["role"] == role)


class TestOwnedRoots:
    def test_relative_root_is_refused(self) -> None:
        with pytest.raises(ArtifactError, match="must be absolute"):
            require_owned_root("relative/root")

    def test_allocation_never_reuses_an_existing_root(
        self, store: SessionStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixed = uuid.UUID(int=7)
        monkeypatch.setattr("apprentice.core.session_store.uuid.uuid4", lambda: fixed)
        first = store.allocate_work_root()
        (first / "implementation.py").write_text("kept = 1\n")

        with pytest.raises(ArtifactError, match="already exists"):
            store.allocate_work_root()
        assert (first / "implementation.py").read_text() == "kept = 1\n"

    def test_run_scope_refuses_a_symlinked_work_root(
        self, store: SessionStore, tmp_path: Path
    ) -> None:
        record = store.create_run("selection", 2)
        work_root = store.run_scope(record).work_root
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        work_root.rmdir()
        work_root.symlink_to(elsewhere)

        with pytest.raises(ArtifactError, match="artifact root is a symlink"):
            store.run_scope(record)

    def test_run_scope_refuses_a_missing_work_root(self, store: SessionStore) -> None:
        record = store.create_run("selection", 2)
        store.run_scope(record).work_root.rmdir()

        with pytest.raises(ArtifactError, match="artifact root does not exist"):
            store.run_scope(record)


class TestRoleWriter:
    def test_hardlinked_target_is_refused_without_touching_the_other_file(
        self, scope: RunScope, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.py"
        outside.write_text("outside = 1\n")
        os.link(outside, scope.work_root / "implementation.py")

        with pytest.raises(ArtifactError, match="single-link regular file"):
            write_role(scope.work_root, "implementation", "replaced = 1\n")
        assert outside.read_text() == "outside = 1\n"

    def test_fifo_target_is_refused(self, scope: RunScope) -> None:
        os.mkfifo(scope.work_root / "scene.py")
        with pytest.raises(ArtifactError):
            write_role(scope.work_root, "manim_scene", "scene = 1\n")

    def test_symlinked_target_is_refused(self, scope: RunScope, tmp_path: Path) -> None:
        outside = tmp_path / "outside.csv"
        outside.write_text("outside\n")
        (scope.work_root / "cards.csv").symlink_to(outside)

        with pytest.raises(ArtifactError, match="refusing to follow symlink"):
            write_role(scope.work_root, "anki_deck", "front,back\n")
        assert outside.read_text() == "outside\n"


class TestSealedBundleVerification:
    def test_removed_file_is_refused(self, store: SessionStore) -> None:
        bundle = _sealed(store)
        (bundle / "cards.csv").unlink()
        with pytest.raises(ArtifactError, match=r"removed=\['cards.csv'\]"):
            load_snapshot(bundle)

    def test_symlinked_artifact_is_refused(self, store: SessionStore, tmp_path: Path) -> None:
        bundle = _sealed(store)
        outside = tmp_path / "scene.py"
        outside.write_bytes((bundle / "scene.py").read_bytes())
        (bundle / "scene.py").unlink()
        (bundle / "scene.py").symlink_to(outside)
        with pytest.raises(ArtifactError, match="refusing to follow symlink"):
            load_snapshot(bundle)

    def test_hardlinked_artifact_is_refused(self, store: SessionStore, tmp_path: Path) -> None:
        bundle = _sealed(store)
        os.link(bundle / "implementation.py", tmp_path / "second-link.py")
        with pytest.raises(ArtifactError, match="single-link regular file"):
            load_snapshot(bundle)

    def test_non_regular_artifact_is_refused(self, store: SessionStore) -> None:
        bundle = _sealed(store)
        (bundle / "cards.csv").unlink()
        os.mkfifo(bundle / "cards.csv")
        with pytest.raises(ArtifactError, match="single-link regular file"):
            load_snapshot(bundle)

    def test_non_canonical_manifest_is_refused(self, store: SessionStore) -> None:
        bundle = _sealed(store)
        path = _writable(bundle / MANIFEST_FILENAME)
        path.write_text(json.dumps(json.loads(path.read_bytes()), indent=2))
        with pytest.raises(ArtifactError, match="not in canonical form"):
            load_snapshot(bundle)

    def test_edited_bytes_under_the_old_claimed_digest_are_refused(
        self, store: SessionStore
    ) -> None:
        bundle = _sealed(store)
        forged = b"impl = 2\n"
        _writable(bundle / "implementation.py").write_bytes(forged)
        path = _writable(bundle / MANIFEST_FILENAME)
        manifest = json.loads(path.read_bytes())
        entry = _entry(manifest, "implementation")
        entry["sha256"] = hashlib.sha256(forged).hexdigest()
        entry["size"] = len(forged)
        path.write_bytes(canonical_json(manifest))

        with pytest.raises(ArtifactError, match="digest does not match"):
            load_snapshot(bundle)

    def test_wrong_size_with_matching_digest_is_refused(self, store: SessionStore) -> None:
        bundle = _sealed(store)
        _rewrite_manifest(bundle, lambda m: _entry(m, "implementation").update(size=1))
        with pytest.raises(ArtifactError, match="bytes differ from its manifest"):
            load_snapshot(bundle)

    def test_retargeted_destination_is_refused(self, store: SessionStore) -> None:
        bundle = _sealed(store)
        _rewrite_manifest(
            bundle,
            lambda m: _entry(m, "implementation")["destination"].update(
                path="01-foundations/microselection.py"
            ),
        )
        with pytest.raises(ArtifactError, match="destination for implementation differs"):
            load_snapshot(bundle)

    def test_unsupported_entry_path_is_refused(self, store: SessionStore) -> None:
        bundle = _sealed(store)
        _writable(bundle / "scene.py").rename(bundle / "other.py")
        _rewrite_manifest(bundle, lambda m: _entry(m, "manim_scene").update(path="other.py"))
        with pytest.raises(ArtifactError, match="unsupported path for manim_scene"):
            load_snapshot(bundle)

    def test_same_size_bytes_with_a_different_digest_are_refused(self, store: SessionStore) -> None:
        bundle = _sealed(store)
        path = _writable(bundle / "implementation.py")
        original = path.read_bytes()
        path.write_bytes(original.replace(b"1", b"2"))
        assert len(path.read_bytes()) == len(original)

        with pytest.raises(ArtifactError, match="implementation bytes differ from its manifest"):
            load_snapshot(bundle)

    @pytest.mark.parametrize(
        ("mutate", "refusal"),
        [
            (lambda m: m.update(extra=1), "does not have the supported field set"),
            (lambda m: m.update(version=2), "unsupported manifest version 2"),
            (
                lambda m: _entry(m, "anki_deck").update(role="slides"),
                "lists unsupported role 'slides'",
            ),
            (lambda m: m["artifacts"].reverse(), "roles are duplicated or unsorted"),
            (
                lambda m: m["artifacts"].append(dict(_entry(m, "manim_scene"))),
                "roles are duplicated or unsorted",
            ),
            (
                lambda m: _entry(m, "implementation").update(mode=420),
                "has a malformed artifact entry",
            ),
            (
                lambda m: _entry(m, "implementation").update(size=float(len(b"impl = 1\n"))),
                "invalid size for implementation",
            ),
            (
                lambda m: _entry(m, "implementation").update(size=-1),
                "invalid size for implementation",
            ),
        ],
    )
    def test_re_signed_manifest_with_an_unsupported_shape_is_refused(
        self, store: SessionStore, mutate: Callable[[dict[str, Any]], None], refusal: str
    ) -> None:
        bundle = _sealed(store)
        _rewrite_manifest(bundle, mutate)
        with pytest.raises(ArtifactError, match=refusal):
            load_snapshot(bundle)

    def test_re_signed_manifest_without_an_implementation_is_refused(
        self, store: SessionStore
    ) -> None:
        bundle = _sealed(store)
        _writable(bundle / "implementation.py").unlink()
        _rewrite_manifest(
            bundle,
            lambda m: m.update(
                artifacts=[e for e in m["artifacts"] if e["role"] != "implementation"]
            ),
        )
        with pytest.raises(ArtifactError, match="has no implementation artifact"):
            load_snapshot(bundle)

    def test_sealing_an_unsupported_role_leaves_nothing_behind(self, store: SessionStore) -> None:
        record = store.create_run("selection", 2)
        bundle_dir = store.bundle_dir(record.run_id)
        before = sorted(p.name for p in bundle_dir.parent.iterdir())

        with pytest.raises(ArtifactError, match=r"cannot seal unsupported roles: \['slides'\]"):
            seal_bundle(
                bundle_dir,
                run_id=record.run_id,
                algorithm="selection",
                tier=2,
                contents={"implementation": "impl = 1\n", "slides": "deck\n"},
            )
        assert sorted(p.name for p in bundle_dir.parent.iterdir()) == before


class _CallbackContext:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state


class TestOwnedRootCallbacks:
    """The drafter and review callbacks write only into the run's own root.

    Budget wiring replaces these callbacks on the assembled pipeline, so they
    are exercised directly here.
    """

    @pytest.fixture
    def shared_temp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        shared = tmp_path / "shared-temp"
        shared.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(shared))
        return shared

    def test_drafter_callback_validates_in_the_run_root(
        self, scope: RunScope, shared_temp: Path
    ) -> None:
        code = fixture_outputs()["drafter"]
        context = _CallbackContext({"generated_code": code})

        anyio.run(_make_after_drafter_callback(scope.work_root), context)

        assert context.state["implementation_path"] == str(scope.work_root / "implementation.py")
        assert (scope.work_root / "implementation.py").read_text() == code
        assert context.state["validation_feedback"] == ""
        assert list(shared_temp.iterdir()) == []

    def test_review_callback_validates_in_the_run_root(
        self, scope: RunScope, shared_temp: Path
    ) -> None:
        outputs = fixture_outputs()
        context = _CallbackContext(
            {
                "generated_code": outputs["drafter"],
                "instrumented_code": outputs["instrumentation"],
                "manim_scene_code": outputs["visualization"],
                "anki_deck_content": outputs["assessment"],
            }
        )
        agent = build_review_agent(
            OfflineFixtureLlm(model="offline-fixture"), scope.work_root, scope.algorithm
        )

        anyio.run(agent.before_agent_callback, context)  # type: ignore[arg-type]

        assert context.state["review_verdict"] == "passed"
        assert sorted(p.name for p in scope.work_root.iterdir()) == [
            "cards.csv",
            "implementation.py",
            "instrumented.py",
            "scene.py",
        ]
        assert list(shared_temp.iterdir()) == []
