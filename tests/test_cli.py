"""Tests for CLI entry point."""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
import pytest

from apprentice.cli import (
    _cmd_approve,
    _cmd_build,
    _cmd_preview,
    _cmd_retry,
    _cmd_submit,
    _run_pipeline,
    main,
)
from apprentice.core.artifacts import canonical_json, manifest_digest
from apprentice.core.config import load_config
from apprentice.core.session_store import RunRecord, SessionStore
from apprentice.models.work_item import BlockingGateError
from tests.conftest import OfflineFixtureLlm, fixture_outputs

if TYPE_CHECKING:
    from collections.abc import Callable

    from apprentice.core.artifacts import RunScope
    from tests.conftest import OfflineRemotes


def _last_json(capsys: pytest.CaptureFixture[str]) -> str:
    """Return the last JSON object the command printed to stdout."""
    out = capsys.readouterr().out
    return out[out.rfind("\n{") + 1 :] if "\n{" in out else out


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
    def __init__(self, algorithm: str, run_id: str, tier: int | None = None) -> None:
        self.algorithm = algorithm
        self.tier = tier
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
        assert set(approval) == {
            "run_id",
            "algorithm",
            "tier",
            "manifest_sha256",
            "approved_by",
            "approved_at",
        }

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


def _approved_run(store_dir: Path) -> RunRecord:
    rec = _make_completed_run(store_dir)
    assert _cmd_approve(_ApproveArgs(rec.run_id)) == 0
    return rec


def _add_file(store_dir: Path, rec: RunRecord) -> None:
    (SessionStore(store_dir=store_dir).bundle_dir(rec.run_id) / "extra.py").write_text("x = 1\n")


def _remove_file(store_dir: Path, rec: RunRecord) -> None:
    (SessionStore(store_dir=store_dir).bundle_dir(rec.run_id) / "cards.csv").unlink()


def _change_bytes(store_dir: Path, rec: RunRecord) -> None:
    _tamper(store_dir, rec.run_id, "implementation.py", b"print('tampered')\n")


def _symlink_artifact(store_dir: Path, rec: RunRecord) -> None:
    path = SessionStore(store_dir=store_dir).bundle_dir(rec.run_id) / "scene.py"
    outside = store_dir.parent / "outside_scene.py"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)


def _approval_of_other_run(store_dir: Path, rec: RunRecord) -> None:
    other = _approved_run(store_dir)
    store = SessionStore(store_dir=store_dir)
    record = store.load(rec.run_id)
    record.approval = store.load(other.run_id).approval
    store.save(record)


def _retarget_destination(store_dir: Path, rec: RunRecord) -> None:
    store = SessionStore(store_dir=store_dir)
    manifest_path = store.bundle_dir(rec.run_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][1]["destination"]["path"] = "01-foundations/microselection.py"
    del manifest["manifest_sha256"]
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest_path.chmod(0o644)
    manifest_path.write_bytes(canonical_json(manifest))


class TestSubmitCommand:
    def test_submit_requires_run_id(self) -> None:
        with pytest.raises(SystemExit, match="2"):
            main(["submit", "selection"])

    def test_submit_blocks_without_approval(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = _make_completed_run(store_dir)

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["remediation"] == f"apprentice approve {rec.run_id}"

    def test_submit_rejects_unknown_run(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _cmd_submit(_SubmitArgs("selection", f"selection-20260101T000000Z-{'0' * 32}")) == 1
        assert "No run record found" in json.loads(capsys.readouterr().out)["error"]

    def test_restarted_submit_promotes_approved_bytes(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rec = _approved_run(store_dir)
        approved = json.loads(capsys.readouterr().out)

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id, tier=2)) == 0

        out = json.loads(capsys.readouterr().out)
        assert out["manifest_sha256"] == approved["manifest_sha256"]
        branch = f"apprentice/{rec.run_id}"
        bundle = SessionStore(store_dir=store_dir).bundle_dir(rec.run_id)
        assert (
            offline_remotes.blob("no-magic-ai/no-magic", branch, "02-alignment/microselection.py")
            == (bundle / "implementation.py").read_bytes()
        )
        assert (
            offline_remotes.blob(
                "no-magic-ai/no-magic-viz", branch, "scenes/scene_microselection.py"
            )
            == (bundle / "scene.py").read_bytes()
        )
        stored = SessionStore(store_dir=store_dir).load(rec.run_id).submission
        assert [r["pr_url"] for r in stored["repositories"]] == [
            r["pr_url"] for r in out["repositories"]
        ]

    def test_already_submitted_run_is_not_published_again(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rec = _approved_run(store_dir)
        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 0
        calls = len(offline_remotes.gh_calls())

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1
        assert "already has a complete submission attempt" in capsys.readouterr().out
        assert len(offline_remotes.gh_calls()) == calls

    def test_full_submission_is_recorded_complete(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        rec = _approved_run(store_dir)
        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 0

        submission = SessionStore(store_dir=store_dir).load(rec.run_id).submission
        assert submission["status"] == "complete"
        assert [(r["pushed"], bool(r["pr_url"])) for r in submission["repositories"]] == [
            (True, True),
            (True, True),
        ]

    @pytest.mark.parametrize(
        ("fail", "status", "effects"),
        [
            ("second_pr", "partial", [(True, True), (True, False)]),
            ("second_push", "partial", [(True, False), (False, False)]),
            ("first_push", "failed", [(False, False), (False, False)]),
        ],
    )
    def test_failed_attempt_is_recorded_and_rerun_after_restart_is_refused(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        fail: str,
        status: str,
        effects: list[tuple[bool, bool]],
    ) -> None:
        rec = _approved_run(store_dir)
        if fail == "second_pr":
            monkeypatch.setenv("OFFLINE_GH_FAIL_ON", "2")
        elif fail == "second_push":
            offline_remotes.reject_pushes("no-magic-ai/no-magic-viz")
        else:
            offline_remotes.reject_pushes("no-magic-ai/no-magic")
        capsys.readouterr()

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        printed = json.loads(capsys.readouterr().out)
        stored = SessionStore(store_dir=store_dir).load(rec.run_id).submission
        assert printed["status"] == stored["status"] == status
        assert [(r["pushed"], bool(r["pr_url"])) for r in stored["repositories"]] == effects
        assert stored["error"] and stored["manifest_sha256"] == rec.manifest_sha256

        monkeypatch.delenv("OFFLINE_GH_FAIL_ON", raising=False)
        branches = {r: offline_remotes.branches(r) for r in offline_remotes.bare}
        gh_calls = len(offline_remotes.gh_calls())
        scratch = sorted((store_dir / "scratch").iterdir())

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        assert f"already has a {status} submission attempt" in capsys.readouterr().out
        assert {r: offline_remotes.branches(r) for r in offline_remotes.bare} == branches
        assert len(offline_remotes.gh_calls()) == gh_calls
        assert sorted((store_dir / "scratch").iterdir()) == scratch
        assert SessionStore(store_dir=store_dir).load(rec.run_id).submission == stored

    def test_interrupted_attempt_stays_pending_and_blocks_rerun(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rec = _approved_run(store_dir)

        def _interrupted(*args: Any) -> Any:
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr("apprentice.agents.packaging.submit_snapshot", _interrupted)
            with pytest.raises(KeyboardInterrupt):
                _cmd_submit(_SubmitArgs("selection", rec.run_id))

        assert SessionStore(store_dir=store_dir).load(rec.run_id).submission["status"] == "pending"
        capsys.readouterr()
        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1
        assert "already has a pending submission attempt" in capsys.readouterr().out
        assert offline_remotes.gh_calls() == []

    @pytest.mark.parametrize(
        ("mutate", "args", "message"),
        [
            (_change_bytes, {}, "differ from its manifest"),
            (_add_file, {}, "added=['extra.py']"),
            (_remove_file, {}, "removed=['cards.csv']"),
            (_symlink_artifact, {}, "refusing to follow symlink"),
            (_approval_of_other_run, {}, "does not match its sealed bundle"),
            (_retarget_destination, {}, "destination for implementation differs"),
            (None, {"algorithm": "quicksort"}, "not 'quicksort'"),
            (None, {"tier": 3}, "not tier 3"),
        ],
    )
    def test_invalid_bundle_or_identity_fails_before_packaging(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        mutate: Any,
        args: dict[str, Any],
        message: str,
    ) -> None:
        rec = _approved_run(store_dir)
        if mutate is not None:
            mutate(store_dir, rec)
        capsys.readouterr()

        submit_args = _SubmitArgs(args.get("algorithm", "selection"), rec.run_id, args.get("tier"))
        assert _cmd_submit(submit_args) == 1

        assert message in json.loads(capsys.readouterr().out)["error"]
        assert not (store_dir / "scratch").exists()
        assert offline_remotes.branches("no-magic-ai/no-magic") == ["main"]
        assert offline_remotes.branches("no-magic-ai/no-magic-viz") == ["main"]
        assert offline_remotes.gh_calls() == []
        assert SessionStore(store_dir=store_dir).load(rec.run_id).submission == {}


class _BuildArgs:
    def __init__(self, algorithm: str = "selection", tier: int = 2) -> None:
        self.algorithm = algorithm
        self.tier = tier
        self.description = ""
        self.backend = None
        self.model = None


class _RetryArgs:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.backend = None
        self.model = None


def _use_model(monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]) -> OfflineFixtureLlm:
    llm = OfflineFixtureLlm(model="offline-fixture", outputs=outputs)
    monkeypatch.setattr("apprentice.cli._resolve_model", lambda cfg, args: llm)
    return llm


def _only_record(store_dir: Path, exclude: set[str] | None = None) -> RunRecord:
    store = SessionStore(store_dir=store_dir)
    (record,) = [r for r in store.list_runs(limit=100) if r.run_id not in (exclude or set())]
    return record


def _assert_halted_with_diagnostics(store_dir: Path, record: RunRecord, gate: str) -> None:
    store = SessionStore(store_dir=store_dir)
    assert record.status == "failed"
    assert record.error == f"blocking gate failed: {gate}"
    stored_verdict = record.session_state["gate_verdicts"][-1]
    tracked_verdict = record.budget_summary["gate_verdicts"][-1]
    assert stored_verdict == tracked_verdict
    assert (stored_verdict["verdict"], stored_verdict["blocking"]) == ("fail", True)
    assert stored_verdict["diagnostics"]
    assert record.session_state["generated_code"]
    assert record.budget_summary["per_agent"]
    assert (store.run_scope(record).work_root / "implementation.py").is_file()
    assert record.manifest_sha256 == ""
    assert not store.bundle_dir(record.run_id).exists()


class TestGateHaltRecording:
    def test_build_halted_by_gate_keeps_state_diagnostics_budget_and_work_files(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        llm = _use_model(monkeypatch, fixture_outputs(failing_implementation=True))

        assert _cmd_build(load_config(None), _BuildArgs()) == 1

        out = json.loads(_last_json(capsys))
        record = _only_record(store_dir)
        _assert_halted_with_diagnostics(store_dir, record, "correctness after implementation")
        assert out["gate"]["gate_name"] == "correctness"
        assert "assessment" not in llm.roles()

    def test_build_with_empty_draft_fails_at_the_correctness_gate(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_model(monkeypatch, fixture_outputs(omit=("drafter",)))

        assert _cmd_build(load_config(None), _BuildArgs()) == 1

        record = _only_record(store_dir)
        assert record.status == "failed"
        assert record.session_state["gate_verdicts"][-1]["diagnostics"] == {
            "error": "implementation_path is empty"
        }
        assert not SessionStore(store_dir=store_dir).bundle_dir(record.run_id).exists()

    def test_passing_build_completes_and_seals(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_model(monkeypatch, fixture_outputs())

        assert _cmd_build(load_config(None), _BuildArgs()) == 0

        record = _only_record(store_dir)
        assert record.status == "completed"
        assert [v["verdict"] for v in record.session_state["gate_verdicts"]] == ["pass"] * 4
        assert SessionStore(store_dir=store_dir).load_bundle(record).artifacts

    def test_retry_halted_by_gate_records_its_state_once(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = SessionStore(store_dir=store_dir)
        previous = store.fail_run(store.create_run("selection", 2), {}, {}, 1.0, "earlier failure")
        _use_model(monkeypatch, fixture_outputs(failing_implementation=True))

        assert _cmd_retry(load_config(None), _RetryArgs(previous.run_id)) == 1

        record = _only_record(store_dir, exclude={previous.run_id})
        _assert_halted_with_diagnostics(store_dir, record, "correctness after implementation")

    def test_integration_runner_records_gate_halt_with_state(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "integration_test", Path(__file__).parent.parent / "scripts" / "integration_test.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        llm = OfflineFixtureLlm(
            model="offline-fixture", outputs=fixture_outputs(failing_implementation=True)
        )
        monkeypatch.setattr("apprentice.providers.factory.create_model", lambda provider: llm)
        store = SessionStore(store_dir=store_dir)

        record = module._run_single(
            "selection", 2, load_config(None), None, None, store, logging.getLogger("t")
        )

        _assert_halted_with_diagnostics(
            store_dir, store.load(record.run_id), "correctness after implementation"
        )


class TestRunnerStateCarrier:
    def test_halt_carries_the_stored_session_state(self, scope: RunScope) -> None:
        from apprentice.core.orchestrator import build_pipeline

        llm = OfflineFixtureLlm(
            model="offline-fixture", outputs=fixture_outputs(failing_implementation=True)
        )
        pipeline = build_pipeline(llm, load_config(None), scope)

        with pytest.raises(BlockingGateError) as excinfo:
            anyio.run(_run_pipeline, pipeline, "selection", 2, "")

        state = excinfo.value.persisted_state()
        assert state["generated_code"] == fixture_outputs(failing_implementation=True)["drafter"]
        assert state["gate_verdicts"][-1] == excinfo.value.verdict


def _tree(root: Path) -> dict[str, tuple[bytes | None, int]]:
    """Every path under `root` with its bytes (files) and mode."""
    return {
        str(path.relative_to(root)): (
            path.read_bytes() if path.is_file() else None,
            path.lstat().st_mode,
        )
        for path in sorted(root.rglob("*"))
    }


def _damage_record(store_dir: Path, run_id: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    path = store_dir / f"{run_id}.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


class TestSealedTierType:
    def test_float_tier_in_a_re_signed_bundle_bound_to_its_record_is_refused_before_packaging(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from apprentice.core.artifacts import MANIFEST_FILENAME

        rec = _approved_run(store_dir)
        path = SessionStore(store_dir=store_dir).bundle_dir(rec.run_id) / MANIFEST_FILENAME
        path.chmod(0o644)
        manifest = json.loads(path.read_bytes())
        del manifest["manifest_sha256"]
        manifest["tier"] = 2.0
        digest = manifest_digest(manifest)
        path.write_bytes(canonical_json({**manifest, "manifest_sha256": digest}))

        def rebind(record: dict[str, Any]) -> None:
            record["manifest_sha256"] = digest
            record["approval"]["manifest_sha256"] = digest

        _damage_record(store_dir, rec.run_id, rebind)
        before = _tree(store_dir)
        capsys.readouterr()

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        assert "unsupported tier 2.0" in json.loads(_last_json(capsys))["error"]
        assert _tree(store_dir) == before
        assert offline_remotes.gh_calls() == []
        assert offline_remotes.branches("no-magic-ai/no-magic") == ["main"]
        assert offline_remotes.branches("no-magic-ai/no-magic-viz") == ["main"]


def _store_with_a_corrupt_record(store_dir: Path) -> Path:
    _make_completed_run(store_dir)
    path = store_dir / ("selection-20990101T000000Z-" + "0" * 32 + ".json")
    path.write_text(json.dumps(["not", "a", "record"]))
    return path


class TestCorruptRecordAtListingConsumers:
    @pytest.mark.parametrize(
        "command",
        [
            lambda: main(["history"]),
            lambda: main(["metrics"]),
            lambda: _cmd_preview(_PreviewArgs()),
        ],
        ids=["history", "metrics", "preview-default"],
    )
    def test_command_reports_the_corrupt_record_and_changes_nothing(
        self,
        store_dir: Path,
        capsys: pytest.CaptureFixture[str],
        command: Callable[[], int],
    ) -> None:
        corrupt = _store_with_a_corrupt_record(store_dir)
        before = _tree(store_dir)
        capsys.readouterr()

        assert command() == 1

        error = json.loads(_last_json(capsys))["error"]
        assert f"corrupt run record {corrupt}" in error
        assert _tree(store_dir) == before

    def test_integration_report_reports_the_corrupt_record(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import importlib.util
        import sys

        corrupt = _store_with_a_corrupt_record(store_dir)
        spec = importlib.util.spec_from_file_location(
            "integration_test", Path(__file__).parent.parent / "scripts" / "integration_test.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(sys, "argv", ["integration_test.py", "--report-only"])
        capsys.readouterr()

        assert module.main() == 1

        assert f"corrupt run record {corrupt}" in capsys.readouterr().err


class TestApprovalRepair:
    def test_non_object_approval_can_be_replaced_by_approve(self, store_dir: Path) -> None:
        rec = _make_completed_run(store_dir)
        _damage_record(store_dir, rec.run_id, lambda r: r.update(approval=["approved"]))

        assert _cmd_approve(_ApproveArgs(rec.run_id)) == 0

        approval = SessionStore(store_dir=store_dir).load(rec.run_id).approval
        assert approval["run_id"] == rec.run_id
        assert approval["approved_by"] == "tester"


def _legacy_record(store_dir: Path, run_id: str, approval: dict[str, Any]) -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        algorithm_name="selection",
        tier=2,
        status="completed",
        session_state=dict(_STATE),
        started_at="2025-01-01T00:00:00+00:00",
        approval=approval,
    )
    return SessionStore(store_dir=store_dir).save(record)


class TestSubmitRemediation:
    @pytest.mark.parametrize(
        "approval",
        [
            {
                "approved_by": "tester",
                "approved_at": "2025-01-01T00:00:00+00:00",
                "run_id": "selection-20250101T000000Z",
                "artifact_hashes": {"implementation": "0" * 64},
            },
            {},
        ],
    )
    def test_run_without_sealed_bundle_gets_rebuild_instruction(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        approval: dict[str, Any],
    ) -> None:
        record = _legacy_record(store_dir, "selection-20250101T000000Z", approval)

        assert _cmd_submit(_SubmitArgs("selection", record.run_id)) == 1

        out = json.loads(capsys.readouterr().out)
        assert "apprentice build selection --tier 2" in out["error"]
        assert "remediation" not in out
        assert not (store_dir / "runs" / record.run_id).exists()
        assert offline_remotes.gh_calls() == []

    def test_sealed_unapproved_run_gets_approve_remediation(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = _make_completed_run(store_dir)

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        assert (
            json.loads(capsys.readouterr().out)["remediation"] == f"apprentice approve {rec.run_id}"
        )


def _sealed_with_verdicts(store_dir: Path, verdicts: list[dict[str, Any]]) -> RunRecord:
    """A completed, sealed run whose build recorded these gate verdicts (pre-fix shape)."""
    rec = _make_completed_run(store_dir)
    rec.budget_summary = {"gate_verdicts": verdicts}
    return SessionStore(store_dir=store_dir).save(rec)


_FAIL = {"gate_name": "correctness", "after_stage": "implementation", "verdict": "fail"}


class TestBlockingGateFailures:
    @pytest.mark.parametrize("verdict", [dict(_FAIL), {**_FAIL, "blocking": True}])
    def test_sealed_run_with_failed_blocking_gate_cannot_be_approved(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str], verdict: dict[str, Any]
    ) -> None:
        rec = _sealed_with_verdicts(store_dir, [verdict])

        assert _cmd_approve(_ApproveArgs(rec.run_id)) == 1

        out = json.loads(capsys.readouterr().out)
        assert "blocking gate failed: correctness after implementation" in out["error"]
        assert out["remediation"] == "apprentice build selection --tier 2"
        assert SessionStore(store_dir=store_dir).load(rec.run_id).approval == {}

    def test_previously_approved_failed_gate_run_is_not_submitted(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rec = _approved_run(store_dir)
        store = SessionStore(store_dir=store_dir)
        record = store.load(rec.run_id)
        record.budget_summary = {"gate_verdicts": [dict(_FAIL)]}
        store.save(record)
        capsys.readouterr()

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        assert "blocking gate failed" in json.loads(capsys.readouterr().out)["error"]
        assert offline_remotes.branches("no-magic-ai/no-magic") == ["main"]
        assert offline_remotes.gh_calls() == []
        assert store.load(rec.run_id).submission == {}

    @pytest.mark.parametrize(
        "verdicts",
        [
            [{"gate_name": "lint", "after_stage": "implementation", "verdict": "warn"}],
            [{**_FAIL, "blocking": False}],
        ],
    )
    def test_warn_and_nonblocking_failures_do_not_block_approval(
        self, store_dir: Path, verdicts: list[dict[str, Any]]
    ) -> None:
        rec = _sealed_with_verdicts(store_dir, verdicts)
        assert _cmd_approve(_ApproveArgs(rec.run_id)) == 0


class TestSubmitPublicationGuards:
    def test_mutated_commit_blob_is_not_pushed_or_reported_submitted(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rec = _approved_run(store_dir)
        offline_remotes.mutate_staged_python("no-magic-ai/no-magic")
        capsys.readouterr()

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        submission = SessionStore(store_dir=store_dir).load(rec.run_id).submission
        assert submission["status"] == "failed"
        assert "differs from approval" in submission["error"]
        assert submission["repositories"] == []
        assert offline_remotes.branches("no-magic-ai/no-magic") == ["main"]
        assert offline_remotes.branches("no-magic-ai/no-magic-viz") == ["main"]
        assert offline_remotes.gh_calls() == []


def _forbidden_generation_modules() -> set[str]:
    """Generation-only modules, including every client the provider factory imports."""
    import apprentice.providers

    forbidden = {
        "google.adk",
        "litellm",
        "apprentice.providers",
        "apprentice.core.orchestrator",
        "apprentice.core.gate_agent",
    }
    for source in Path(apprentice.providers.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
                else []
            )
            for name in names:
                parts = name.split(".")
                if parts[0] in sys.stdlib_module_names or parts[0] in ("apprentice", "__future__"):
                    continue
                forbidden.add(".".join(parts[:2]) if parts[0] == "google" else parts[0])
    return forbidden


class _CountingHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        self.requests.append(self.path)
        self.send_response(503)
        self.end_headers()

    def do_GET(self) -> None:
        self.do_POST()


class TestFreshSubmitProcess:
    def test_submit_in_a_fresh_interpreter_loads_no_generation_module_and_calls_no_model(
        self, tmp_path: Path, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        sessions = home / ".apprentice" / "sessions"
        monkeypatch.setattr("apprentice.core.session_store._DEFAULT_STORE_DIR", sessions)
        rec = _approved_run(sessions)
        _CountingHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        root = Path(__file__).parent.parent
        config = (root / "config" / "apprentice.toml").read_text(encoding="utf-8")
        config = config.replace('backend = "openai"', 'backend = "local"').replace(
            'local_api_base = ""', f'local_api_base = "http://127.0.0.1:{server.server_port}/v1"'
        )
        config = config.replace('"${HOME}/.apprentice/logs"', f'"{home}/logs"')
        config_path = tmp_path / "apprentice.toml"
        config_path.write_text(config, encoding="utf-8")
        forbidden = sorted(_forbidden_generation_modules())
        script = (
            "import json, sys\n"
            "from apprentice.cli import main\n"
            f"code = main(['--config', {str(config_path)!r}, 'submit', 'selection', '--run-id', {rec.run_id!r}])\n"
            f"forbidden = {forbidden!r}\n"
            "loaded = sorted(m for m in sys.modules if any(m == f or m.startswith(f + '.') for f in forbidden))\n"
            "print('FRESH-SUBMIT ' + json.dumps({'code': code, 'loaded': loaded}))\n"
        )
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "PYTHONPATH": str(root / "src"),
            "GIT_CONFIG_GLOBAL": os.environ["GIT_CONFIG_GLOBAL"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
        }
        try:
            result = subprocess.run(
                [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False
            )
        finally:
            server.shutdown()

        line = next(x for x in result.stdout.splitlines() if x.startswith("FRESH-SUBMIT "))
        outcome = json.loads(line.removeprefix("FRESH-SUBMIT "))
        assert outcome == {"code": 0, "loaded": []}, result.stderr[-2000:]
        assert _CountingHandler.requests == []
        assert {"google.adk", "litellm", "openai", "anthropic"} <= set(forbidden)
        assert offline_remotes.branches("no-magic-ai/no-magic") == [
            f"apprentice/{rec.run_id}",
            "main",
        ]
