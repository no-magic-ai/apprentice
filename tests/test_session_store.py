"""Tests for session persistence and run record management."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from apprentice.core.artifacts import MANIFEST_FILENAME, ArtifactError, canonical_json
from apprentice.core.session_store import RunRecord, SessionStore

if TYPE_CHECKING:
    from pathlib import Path


class TestRunRecord:
    def test_to_dict_round_trip(self) -> None:
        record = RunRecord(
            run_id="test-123",
            algorithm_name="quicksort",
            tier=2,
            status="completed",
            session_state={"generated_code": "print('hello')"},
            budget_summary={"tokens_used": 100},
            started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:00",
            elapsed_seconds=60.0,
        )
        d = record.to_dict()
        restored = RunRecord.from_dict(d)
        assert restored.run_id == "test-123"
        assert restored.algorithm_name == "quicksort"
        assert restored.tier == 2
        assert restored.status == "completed"
        assert restored.session_state == {"generated_code": "print('hello')"}
        assert restored.elapsed_seconds == 60.0

    def test_defaults(self) -> None:
        record = RunRecord(
            run_id="test",
            algorithm_name="algo",
            tier=1,
            status="in_progress",
        )
        assert record.session_state == {}
        assert record.budget_summary == {}
        assert record.error == ""
        assert record.elapsed_seconds == 0.0


class TestSessionStore:
    def test_create_run(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("quicksort", 2)
        assert record.algorithm_name == "quicksort"
        assert record.tier == 2
        assert record.status == "in_progress"
        assert record.started_at != ""
        assert record.run_id.startswith("quicksort-")

    def test_complete_run(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("quicksort", 2)
        completed = store.complete_run(
            record,
            session_state={"generated_code": "code"},
            budget_summary={"tokens_used": 500},
            elapsed=10.5,
        )
        assert completed.status == "completed"
        assert completed.session_state == {"generated_code": "code"}
        assert completed.elapsed_seconds == 10.5
        assert completed.completed_at != ""

    def test_fail_run(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("quicksort", 2)
        failed = store.fail_run(
            record,
            session_state={"partial": "data"},
            budget_summary={},
            elapsed=5.0,
            error="model returned empty response",
        )
        assert failed.status == "failed"
        assert failed.error == "model returned empty response"
        assert failed.session_state == {"partial": "data"}

    def test_load(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("quicksort", 2)
        loaded = store.load(record.run_id)
        assert loaded.run_id == record.run_id
        assert loaded.algorithm_name == "quicksort"

    def test_load_not_found(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            store.load(f"quicksort-20260101T000000Z-{'0' * 32}")

    def test_list_runs(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        store.create_run("algo1", 1)
        store.create_run("algo2", 2)
        store.create_run("algo3", 3)
        runs = store.list_runs()
        assert len(runs) == 3

    def test_list_runs_filter_status(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        r1 = store.create_run("algo1", 1)
        r2 = store.create_run("algo2", 2)
        store.complete_run(r1, {"generated_code": "code"}, {}, 1.0)
        store.fail_run(r2, {}, {}, 1.0, "error")

        completed = store.list_runs(status="completed")
        assert len(completed) == 1
        assert completed[0].status == "completed"

        failed = store.list_runs(status="failed")
        assert len(failed) == 1
        assert failed[0].status == "failed"

    def test_list_runs_limit(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        for i in range(5):
            store.create_run(f"algo{i}", 1)
        runs = store.list_runs(limit=3)
        assert len(runs) == 3

    def test_delete(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("quicksort", 2)
        assert store.delete(record.run_id) is True
        assert store.delete(record.run_id) is False

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        assert store.delete("quicksort-20260101T000000Z") is False

    def test_store_dir_created(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "nested" / "sessions"
        store = SessionStore(store_dir=store_dir)
        assert store.store_dir.exists()


class _FrozenClock:
    """Stands in for `datetime` so every run starts in the same UTC second."""

    moment = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls.moment

    fromisoformat = staticmethod(datetime.fromisoformat)


def _write_legacy_record(store: SessionStore, run_id: str, started_at: str) -> None:
    record = {
        "run_id": run_id,
        "algorithm_name": "selection",
        "tier": 2,
        "status": "completed",
        "session_state": {"generated_code": "legacy = 1\n"},
        "budget_summary": {},
        "started_at": started_at,
        "completed_at": started_at,
        "error": "",
        "elapsed_seconds": 1.0,
    }
    (store.store_dir / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")


class TestRunOwnership:
    def test_same_second_runs_get_distinct_ids_records_and_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("apprentice.core.session_store.datetime", _FrozenClock)
        store = SessionStore(store_dir=tmp_path)

        first = store.create_run("selection", 2)
        store.complete_run(first, {"generated_code": "first = 1\n"}, {}, 1.0)
        second = store.create_run("selection", 2)

        assert first.run_id != second.run_id
        assert store.load(first.run_id).status == "completed"
        assert store.load(second.run_id).status == "in_progress"
        assert store.run_scope(first).work_root != store.run_scope(second).work_root

    def test_completed_bundles_of_same_algorithm_stay_independent(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        first = store.create_run("selection", 2)
        second = store.create_run("selection", 2)

        store.complete_run(first, {"generated_code": "first = 1\n"}, {}, 1.0)
        store.complete_run(second, {"generated_code": "second = 2\n"}, {}, 1.0)

        first_bytes = store.load_bundle(store.load(first.run_id)).artifacts[0].data
        second_bytes = store.load_bundle(store.load(second.run_id)).artifacts[0].data
        assert (first_bytes, second_bytes) == (b"first = 1\n", b"second = 2\n")

    def test_create_run_rejects_unsafe_algorithm_and_tier(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        with pytest.raises(ArtifactError, match="algorithm name"):
            store.create_run("../escape", 2)
        with pytest.raises(ArtifactError, match="tier"):
            store.create_run("selection", 5)
        assert list(tmp_path.glob("*.json")) == []

    def test_allocated_work_roots_are_exclusive(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        first = store.allocate_work_root()
        second = store.allocate_work_root()
        assert first != second
        assert first.is_dir() and second.is_dir()


class TestRunLookupAndOrdering:
    def test_lookup_is_exact_without_path_aliases(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("selection", 2)
        alias = record.run_id.replace("selection", "selection/..", 1)
        with pytest.raises(ValueError, match="invalid run ID"):
            store.load(alias)
        with pytest.raises(ValueError, match="invalid run ID"):
            store.load("../selection-20260101T000000Z")

    def test_exact_legacy_id_still_loads(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        _write_legacy_record(store, "selection-20250101T000000Z", "2025-01-01T00:00:00+00:00")
        assert store.load("selection-20250101T000000Z").algorithm_name == "selection"

    def test_record_with_mismatched_identity_is_rejected(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        _write_legacy_record(store, "selection-20250101T000000Z", "2025-01-01T00:00:00+00:00")
        (tmp_path / "selection-20250101T000000Z.json").rename(
            tmp_path / "other-20250101T000000Z.json"
        )
        with pytest.raises(ValueError, match="different run ID"):
            store.load("other-20250101T000000Z")

    def test_history_orders_by_started_at_not_filename(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        newer = store.create_run("aardvark", 2)
        _write_legacy_record(store, "zebra-20990101T000000Z", "2020-01-01T00:00:00+00:00")

        runs = store.list_runs()

        assert [r.run_id for r in runs] == [newer.run_id, "zebra-20990101T000000Z"]

    def test_same_started_at_ties_break_deterministically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("apprentice.core.session_store.datetime", _FrozenClock)
        store = SessionStore(store_dir=tmp_path)
        ids = {store.create_run("selection", 2).run_id for _ in range(3)}

        assert [r.run_id for r in store.list_runs()] == sorted(ids, reverse=True)


class TestSealedBundle:
    def test_complete_run_seals_read_only_canonical_bundle(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("selection", 3)
        store.complete_run(
            record,
            {"generated_code": "impl = 1\n", "anki_deck_content": "front,back\n"},
            {},
            1.0,
        )

        bundle = store.bundle_dir(record.run_id)
        manifest = json.loads((bundle / MANIFEST_FILENAME).read_bytes())
        assert (bundle / MANIFEST_FILENAME).read_bytes() == canonical_json(manifest)
        assert manifest["manifest_sha256"] == store.load(record.run_id).manifest_sha256
        assert [a["role"] for a in manifest["artifacts"]] == ["anki_deck", "implementation"]
        assert manifest["artifacts"][1]["destination"] == {
            "repository": "no-magic-ai/no-magic",
            "path": "03-systems/microselection.py",
        }
        assert manifest["artifacts"][0]["destination"] is None
        mode = (bundle / "implementation.py").stat().st_mode
        assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)

    def test_completion_without_implementation_fails_and_stays_in_progress(
        self, tmp_path: Path
    ) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("selection", 2)
        with pytest.raises(ArtifactError, match="no implementation"):
            store.complete_run(record, {"manim_scene_code": "scene = 1\n"}, {}, 1.0)
        assert store.load(record.run_id).status == "in_progress"

    def test_sealed_bundle_is_never_replaced(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        record = store.create_run("selection", 2)
        store.complete_run(record, {"generated_code": "impl = 1\n"}, {}, 1.0)
        with pytest.raises(ArtifactError, match="already has a sealed bundle"):
            store.complete_run(record, {"generated_code": "impl = 2\n"}, {}, 1.0)
        assert store.load_bundle(record).artifacts[0].data == b"impl = 1\n"

    def test_legacy_run_without_bundle_requires_rebuild(self, tmp_path: Path) -> None:
        store = SessionStore(store_dir=tmp_path)
        _write_legacy_record(store, "selection-20250101T000000Z", "2025-01-01T00:00:00+00:00")
        record = store.load("selection-20250101T000000Z")

        with pytest.raises(ArtifactError, match="apprentice build selection --tier 2"):
            store.load_bundle(record)
        assert not store.bundle_dir(record.run_id).exists()
