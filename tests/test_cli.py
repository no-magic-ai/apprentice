"""Tests for CLI entry point."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from apprentice.cli import _cmd_approve, _cmd_preview, _cmd_submit, main
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


class _PreviewArgs:
    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id


class _SubmitArgs:
    def __init__(self, algorithm: str, run_id: str | None) -> None:
        self.algorithm = algorithm
        self.tier = 2
        self.backend = None
        self.model = None
        self.run_id = run_id


_STATE = {
    "algorithm_name": "selection",
    "generated_code": "print('hi')\n",
    "manim_scene_code": "print('scene')\n",
    "anki_deck_content": "front,back\n",
}


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "sessions"
    monkeypatch.setattr("apprentice.core.session_store._DEFAULT_STORE_DIR", directory)
    return directory


def _make_completed_run(store_dir: Path, algorithm: str = "selection") -> RunRecord:
    store = SessionStore(store_dir=store_dir)
    rec = store.create_run(algorithm, tier=2)
    return store.complete_run(rec, session_state=dict(_STATE), budget_summary={}, elapsed=1.0)


def _tamper(store_dir: Path, run_id: str, filename: str, data: bytes) -> None:
    path = SessionStore(store_dir=store_dir).bundle_dir(run_id) / filename
    path.chmod(0o644)
    path.write_bytes(data)


class TestApproveCommand:
    def test_approve_binds_run_identity_manifest_and_destinations(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = _make_completed_run(store_dir)

        code = _cmd_approve(_ApproveArgs(rec.run_id))
        assert code == 0

        out = json.loads(capsys.readouterr().out)
        assert out["approved"] is True
        assert (out["run_id"], out["algorithm"], out["tier"]) == (rec.run_id, "selection", 2)
        assert out["manifest_sha256"] == rec.manifest_sha256
        destinations = {a["role"]: a["destination"] for a in out["artifacts"]}
        assert destinations == {
            "anki_deck": None,
            "implementation": {
                "repository": "no-magic-ai/no-magic",
                "path": "02-alignment/microselection.py",
            },
            "manim_scene": {
                "repository": "no-magic-ai/no-magic-viz",
                "path": "scenes/scene_microselection.py",
            },
        }
        approval = SessionStore(store_dir=store_dir).load(rec.run_id).approval
        assert approval["manifest_sha256"] == rec.manifest_sha256
        assert approval["approved_by"] == "tester"
        assert set(approval["artifact_hashes"]) == {"anki_deck", "implementation", "manim_scene"}

    def test_approve_rejects_tampered_bundle(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = _make_completed_run(store_dir)
        _tamper(store_dir, rec.run_id, "implementation.py", b"print('tampered')\n")

        assert _cmd_approve(_ApproveArgs(rec.run_id)) == 1
        assert "differ from its manifest" in json.loads(capsys.readouterr().out)["error"]
        assert SessionStore(store_dir=store_dir).load(rec.run_id).approval == {}

    def test_approve_legacy_run_requires_rebuild(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = SessionStore(store_dir=store_dir)
        legacy = RunRecord(
            run_id="selection-20250101T000000Z",
            algorithm_name="selection",
            tier=2,
            status="completed",
            session_state=dict(_STATE),
            started_at="2025-01-01T00:00:00+00:00",
        )
        store.save(legacy)

        assert _cmd_approve(_ApproveArgs(legacy.run_id)) == 1
        assert "apprentice build selection --tier 2" in json.loads(capsys.readouterr().out)["error"]
        assert not store.bundle_dir(legacy.run_id).exists()

    def test_approve_rejects_missing_run(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _cmd_approve(_ApproveArgs(f"selection-20260101T000000Z-{'0' * 32}"))
        assert code == 1
        assert "No run record found" in capsys.readouterr().out

    def test_approve_rejects_invalid_run_id(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _cmd_approve(_ApproveArgs("../does-not-exist")) == 1
        assert "invalid run ID" in capsys.readouterr().out


class TestPreviewCommand:
    def test_preview_reads_sealed_bundle_after_store_restart(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        older = _make_completed_run(store_dir)
        newer = _make_completed_run(store_dir)

        assert _cmd_preview(_PreviewArgs()) == 0
        latest = json.loads(capsys.readouterr().out)
        assert latest["run_id"] == newer.run_id

        assert _cmd_preview(_PreviewArgs(older.run_id)) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["run_id"] == older.run_id
        assert out["manifest_sha256"] == older.manifest_sha256
        previews = {a["role"]: a["preview"] for a in out["artifacts"]}
        assert previews["implementation"] == "print('hi')\n"

    def test_preview_rejects_added_file(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = _make_completed_run(store_dir)
        bundle = SessionStore(store_dir=store_dir).bundle_dir(rec.run_id)
        (bundle / "extra.py").write_text("x = 1\n")

        assert _cmd_preview(_PreviewArgs(rec.run_id)) == 1
        assert "added=['extra.py']" in json.loads(capsys.readouterr().out)["error"]


class TestSubmitGuard:
    def test_submit_blocks_without_approval(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _make_completed_run(store_dir)

        code = _cmd_submit(cfg=None, args=_SubmitArgs("selection", run_id=None))
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert "apprentice approve" in out["remediation"]

    def test_submit_requires_completed_run(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _cmd_submit(cfg=None, args=_SubmitArgs("no_such_algo", run_id=None))
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert "No completed build" in out["error"]

    def test_submit_regenerates_into_fresh_root_never_the_sealed_bundle(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from google.adk.models.lite_llm import LiteLlm

        from apprentice.core.config import load_config
        from apprentice.core.gate_agent import GateAgent
        from apprentice.gates.review import ReviewGate

        rec = _make_completed_run(store_dir)
        assert _cmd_approve(_ApproveArgs(rec.run_id)) == 0
        store = SessionStore(store_dir=store_dir)
        sealed = store.load_bundle(store.load(rec.run_id))
        captured: list[Any] = []

        async def _record_pipeline(pipeline: Any, *args: Any) -> dict[str, Any]:
            captured.append(pipeline)
            return {}

        monkeypatch.setattr(
            "apprentice.cli._resolve_model", lambda cfg, args: LiteLlm(model="openai/fixture")
        )
        monkeypatch.setattr("apprentice.cli._run_pipeline", _record_pipeline)

        assert _cmd_submit(load_config(None), _SubmitArgs("selection", rec.run_id)) == 0

        gates = [a for a in captured[0].sub_agents if isinstance(a, GateAgent)]
        roots = {g.scope.work_root for g in gates}
        assert len(roots) == 1
        regen_root = roots.pop()
        assert regen_root not in {
            store.bundle_dir(rec.run_id),
            store.run_scope(store.load(rec.run_id)).work_root,
        }
        assert list(regen_root.iterdir()) == []
        review = next(g.gate for g in gates if isinstance(g.gate, ReviewGate))
        assert review._approval == store.load(rec.run_id).approval
        assert store.load_bundle(store.load(rec.run_id)) == sealed
