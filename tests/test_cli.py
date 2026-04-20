"""Tests for CLI entry point."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from apprentice.cli import _cmd_approve, _cmd_submit, main
from apprentice.core.session_store import RunRecord, SessionStore

if TYPE_CHECKING:
    from pathlib import Path


class TestCLI:
    def test_version(self, capsys: object) -> None:
        import pytest

        with pytest.raises(SystemExit, match="0"):
            main(["--version"])

    def test_no_command_returns_1(self) -> None:
        result = main([])
        assert result == 1

    def test_config_command(self) -> None:
        result = main(["config"])
        assert result == 0

    def test_status_command(self) -> None:
        result = main(["status"])
        assert result == 0


class _ApproveArgs:
    def __init__(self, run_id: str, approver: str | None = "tester") -> None:
        self.run_id = run_id
        self.approver = approver


class _SubmitArgs:
    def __init__(self, algorithm: str, run_id: str | None) -> None:
        self.algorithm = algorithm
        self.tier = 2
        self.backend = None
        self.model = None
        self.run_id = run_id


def _make_completed_run(store_dir: Path, algorithm: str) -> RunRecord:
    store = SessionStore(store_dir=store_dir)
    rec = store.create_run(algorithm, tier=2)
    rec = store.complete_run(
        rec,
        session_state={
            "algorithm_name": algorithm,
            "generated_code": "print('hi')\n",
        },
        budget_summary={},
        elapsed=1.0,
    )
    return rec


class TestApproveCommand:
    def test_approve_writes_approval_with_hashes(
        self, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        monkeypatch.setattr(
            "apprentice.core.session_store._DEFAULT_STORE_DIR", tmp_path / "sessions"
        )
        monkeypatch.setattr(
            "apprentice.core.gate_agent.tempfile.gettempdir", lambda: str(tmp_path)
        )
        rec = _make_completed_run(tmp_path / "sessions", "selection")

        code = _cmd_approve(_ApproveArgs(rec.run_id))
        assert code == 0

        out = json.loads(capsys.readouterr().out)
        assert out["approved"] is True
        assert out["approved_by"] == "tester"
        assert "implementation" in out["artifact_hashes"]

        store = SessionStore(store_dir=tmp_path / "sessions")
        updated = store.load(rec.run_id)
        approval = updated.session_state["review_approval"]
        assert approval["approved_by"] == "tester"
        assert approval["run_id"] == rec.run_id

    def test_approve_rejects_missing_run(self, tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
        monkeypatch.setattr(
            "apprentice.core.session_store._DEFAULT_STORE_DIR", tmp_path / "sessions"
        )
        code = _cmd_approve(_ApproveArgs("does-not-exist"))
        assert code == 1
        assert "Run not found" in capsys.readouterr().out


class TestSubmitGuard:
    def test_submit_blocks_without_approval(
        self, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        monkeypatch.setattr(
            "apprentice.core.session_store._DEFAULT_STORE_DIR", tmp_path / "sessions"
        )
        monkeypatch.setattr(
            "apprentice.core.gate_agent.tempfile.gettempdir", lambda: str(tmp_path)
        )
        _make_completed_run(tmp_path / "sessions", "selection")

        code = _cmd_submit(cfg=None, args=_SubmitArgs("selection", run_id=None))
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert "apprentice approve" in out["remediation"]

    def test_submit_requires_completed_run(
        self, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        monkeypatch.setattr(
            "apprentice.core.session_store._DEFAULT_STORE_DIR", tmp_path / "sessions"
        )
        code = _cmd_submit(cfg=None, args=_SubmitArgs("no-such-algo", run_id=None))
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert "No completed build" in out["error"]
