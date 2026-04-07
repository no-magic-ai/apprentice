"""Tests for session persistence and run record management."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
        import pytest

        store = SessionStore(store_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            store.load("nonexistent-run")

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
        store.complete_run(r1, {}, {}, 1.0)
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
        assert store.delete("nonexistent") is False

    def test_store_dir_created(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "nested" / "sessions"
        store = SessionStore(store_dir=store_dir)
        assert store.store_dir.exists()
