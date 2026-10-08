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

import pytest

from apprentice.cli import (
    _cmd_approve,
    _cmd_preview,
    _cmd_submit,
    main,
)
from apprentice.core.artifacts import canonical_json, manifest_digest
from apprentice.core.config import load_config
from apprentice.core.session_store import RunRecord, SessionStore, default_store_dir
from tests.conftest import fixture_outputs
from tests.responses_fixture import (
    ResponsesFixture,
    ResponsesServer,
    local_profile,
    role_of,
    write_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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
def store_dir() -> Path:
    """The default store root under the test's private HOME, used by CLI commands."""
    return default_store_dir()


_CORE_PR = "https://github.com/no-magic-ai/no-magic/pull/offline-1"
_VIZ_PR = "https://github.com/no-magic-ai/no-magic-viz/pull/offline-2"


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

        stored = SessionStore(store_dir=store_dir).load(rec.run_id).submission
        capsys.readouterr()

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1
        out = json.loads(_last_json(capsys))
        assert "already has a submission attempt" in out["error"]
        assert out["submission"] == stored
        assert out["submission"]["status"] == "complete"
        assert len(offline_remotes.gh_calls()) == calls

    def test_full_submission_is_recorded_complete(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        rec = _approved_run(store_dir)
        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 0

        submission = SessionStore(store_dir=store_dir).load(rec.run_id).submission
        assert submission["status"] == "complete"
        assert [(r["pushed"], r["pr_url"]) for r in submission["repositories"]] == [
            (True, _CORE_PR),
            (True, _VIZ_PR),
        ]

    @pytest.mark.parametrize(
        ("fail", "status", "effects"),
        [
            ("second_pr", "partial", [(True, _CORE_PR), (True, "")]),
            ("second_push", "partial", [(True, ""), (False, "")]),
            ("first_push", "failed", [(False, ""), (False, "")]),
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
        effects: list[tuple[bool, str]],
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
        assert [(r["pushed"], r["pr_url"]) for r in stored["repositories"]] == effects
        assert stored["error"] and stored["manifest_sha256"] == rec.manifest_sha256

        monkeypatch.delenv("OFFLINE_GH_FAIL_ON", raising=False)
        branches = {r: offline_remotes.branches(r) for r in offline_remotes.bare}
        gh_calls = len(offline_remotes.gh_calls())
        scratch = sorted((store_dir / "scratch").iterdir())

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        out = json.loads(_last_json(capsys))
        assert "already has a submission attempt" in out["error"]
        assert out["submission"]["status"] == status
        assert {r: offline_remotes.branches(r) for r in offline_remotes.bare} == branches
        assert len(offline_remotes.gh_calls()) == gh_calls
        assert sorted((store_dir / "scratch").iterdir()) == scratch
        assert SessionStore(store_dir=store_dir).load(rec.run_id).submission == stored

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


@pytest.fixture
def toy_config(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[Path, ResponsesFixture]]:
    """A complete config whose route is the loopback toy model (zero-hosted profile).

    An indirect parameter (a dict) overrides config limits for that test.
    """
    fixture = ResponsesFixture()
    with ResponsesServer(fixture) as server:
        config = write_config(
            tmp_path / "apprentice.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            **getattr(request, "param", {}),
        )
        yield config, fixture


def _generated_roles(fixture: ResponsesFixture) -> list[str]:
    return [role_of(body) for body in fixture.generations()]


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
    assert record.budget_summary["accounting"]["entries"]
    assert (store.run_scope(record).work_root / "implementation.py").is_file()
    assert record.manifest_sha256 == ""
    assert not store.bundle_dir(record.run_id).exists()


@pytest.mark.usefixtures("judged_execution")
class TestGateHaltRecording:
    def test_build_halted_by_gate_keeps_state_diagnostics_budget_and_work_files(
        self,
        store_dir: Path,
        toy_config: tuple[Path, ResponsesFixture],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config, fixture = toy_config
        fixture.outputs = fixture_outputs(failing_implementation=True)

        assert main(["--config", str(config), "build", "selection"]) == 1

        out = json.loads(_last_json(capsys))
        record = _only_record(store_dir)
        _assert_halted_with_diagnostics(store_dir, record, "correctness after implementation")
        assert out["gate"]["gate_name"] == "correctness"
        assert record.session_state["generated_code"] == fixture.outputs["drafter"]
        assert _generated_roles(fixture) == ["drafter"] * 3

    def test_build_with_empty_draft_fails_at_the_correctness_gate(
        self, store_dir: Path, toy_config: tuple[Path, ResponsesFixture]
    ) -> None:
        config, fixture = toy_config
        fixture.outputs = fixture_outputs(omit=("drafter",))

        assert main(["--config", str(config), "build", "selection"]) == 1

        record = _only_record(store_dir)
        assert record.status == "failed"
        assert record.session_state["gate_verdicts"][-1]["diagnostics"] == {
            "error": "implementation_path is empty"
        }
        assert not SessionStore(store_dir=store_dir).bundle_dir(record.run_id).exists()

    def test_retry_halted_by_gate_records_its_state_once(
        self, store_dir: Path, toy_config: tuple[Path, ResponsesFixture]
    ) -> None:
        config, fixture = toy_config
        fixture.outputs = fixture_outputs(omit=("drafter",))
        assert main(["--config", str(config), "build", "selection"]) == 1
        previous = _only_record(store_dir)
        fixture.outputs = fixture_outputs(failing_implementation=True)

        assert main(["--config", str(config), "retry", previous.run_id]) == 1

        record = _only_record(store_dir, exclude={previous.run_id})
        _assert_halted_with_diagnostics(store_dir, record, "correctness after implementation")

    def test_integration_runner_records_gate_halt_with_state(
        self, store_dir: Path, toy_config: tuple[Path, ResponsesFixture]
    ) -> None:
        import importlib.util

        from apprentice.controls.footprint import Footprint
        from apprentice.controls.policy import ControlPolicy
        from apprentice.core.cycles import open_authority
        from apprentice.providers.factory import resolve_route

        spec = importlib.util.spec_from_file_location(
            "integration_test", Path(__file__).parent.parent / "scripts" / "integration_test.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config, fixture = toy_config
        fixture.outputs = fixture_outputs(failing_implementation=True)
        cfg = load_config(config)
        store = SessionStore(store_dir=store_dir)
        authority = open_authority(store, Footprint(existing=()))

        record = module._run_single(
            "selection",
            2,
            store,
            authority,
            resolve_route(cfg.provider),
            ControlPolicy.from_config(cfg),
            logging.getLogger("t"),
        )

        _assert_halted_with_diagnostics(
            store_dir, store.load(record.run_id), "correctness after implementation"
        )


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
        "apprentice.metering.client",
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


# Fixed driver (argv: config, run ID, forbidden modules as JSON).
_FRESH_SUBMIT = """
import json, sys

from apprentice.cli import main

config, run_id, forbidden = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
code = main(["--config", config, "submit", "selection", "--run-id", run_id])
loaded = sorted(m for m in sys.modules if any(m == f or m.startswith(f + ".") for f in forbidden))
print("FRESH-SUBMIT " + json.dumps({"code": code, "loaded": loaded}))
"""


class TestFreshSubmitProcess:
    def test_submit_in_a_fresh_interpreter_loads_no_generation_module_and_calls_no_model(
        self, tmp_path: Path, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        sessions = home / ".apprentice" / "sessions"
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
        argv = [str(config_path), rec.run_id, json.dumps(forbidden)]
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
                [sys.executable, "-c", _FRESH_SUBMIT, *argv],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            server.shutdown()

        line = next(x for x in result.stdout.splitlines() if x.startswith("FRESH-SUBMIT "))
        outcome = json.loads(line.removeprefix("FRESH-SUBMIT "))
        assert outcome == {"code": 0, "loaded": []}, result.stderr[-2000:]
        assert _CountingHandler.requests == []
        assert {"google.adk", "litellm", "openai", "httpx"} <= set(forbidden)
        assert offline_remotes.branches("no-magic-ai/no-magic") == [
            f"apprentice/{rec.run_id}",
            "main",
        ]


class TestApproverSelection:
    @pytest.mark.parametrize("approver", ["", "   ", "a\nb", "a\rb", "a\x00b"])
    def test_invalid_explicit_approver_is_refused_without_falling_back(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        approver: str,
    ) -> None:
        rec = _make_completed_run(store_dir)
        monkeypatch.setenv("GITHUB_USER", "environment-reviewer")
        record_bytes = (store_dir / f"{rec.run_id}.json").read_bytes()

        assert _cmd_approve(_ApproveArgs(rec.run_id, approver=approver)) == 1

        assert "invalid approver" in json.loads(capsys.readouterr().out)["error"]
        assert (store_dir / f"{rec.run_id}.json").read_bytes() == record_bytes
        assert not (store_dir / "runs" / f"{rec.run_id}.lock").exists()

    def test_accepted_approver_is_stored_exactly_as_given(self, store_dir: Path) -> None:
        rec = _make_completed_run(store_dir)

        assert _cmd_approve(_ApproveArgs(rec.run_id, approver="  Ada Lovelace ")) == 0

        approval = SessionStore(store_dir=store_dir).load(rec.run_id).approval
        assert approval["approved_by"] == "  Ada Lovelace "

    def test_environment_approver_is_used_when_none_is_given(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _make_completed_run(store_dir)
        monkeypatch.setenv("GITHUB_USER", "environment-reviewer")

        assert _cmd_approve(_ApproveArgs(rec.run_id, approver=None)) == 0

        approval = SessionStore(store_dir=store_dir).load(rec.run_id).approval
        assert approval["approved_by"] == "environment-reviewer"

    def test_invalid_environment_approver_is_refused(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rec = _make_completed_run(store_dir)
        monkeypatch.setenv("GITHUB_USER", "line\nbreak")
        monkeypatch.setenv("USER", "fallback-user")

        assert _cmd_approve(_ApproveArgs(rec.run_id, approver=None)) == 1

        assert "invalid approver" in json.loads(capsys.readouterr().out)["error"]
        assert SessionStore(store_dir=store_dir).load(rec.run_id).approval == {}


def _store_approval_field(store_dir: Path, run_id: str, field: str, value: object) -> None:
    path = store_dir / f"{run_id}.json"
    data = json.loads(path.read_bytes())
    data["approval"][field] = value
    path.write_text(json.dumps(data))


class TestStoredApprovalMetadata:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("approved_at", ""),
            ("approved_at", 1759795200),
            ("approved_at", "yesterday"),
            ("approved_at", "2026-10-07T08:09:10"),
            ("approved_at", "2026-10-07 08:09:10+00:00"),
            ("approved_at", "2026-10-07T08:09:10Z"),
            ("approved_by", ""),
            ("approved_by", 7),
            ("approved_by", "reviewer\nForged-Trailer: yes"),
        ],
    )
    def test_malformed_approval_is_refused_before_any_attempt(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        field: str,
        value: object,
    ) -> None:
        rec = _approved_run(store_dir)
        _store_approval_field(store_dir, rec.run_id, field, value)
        record_bytes = (store_dir / f"{rec.run_id}.json").read_bytes()
        capsys.readouterr()

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        out = json.loads(capsys.readouterr().out)
        assert f"approval of run {rec.run_id} is malformed" in out["error"]
        assert out["remediation"].endswith(f"apprentice approve {rec.run_id}`")
        assert (store_dir / f"{rec.run_id}.json").read_bytes() == record_bytes
        assert not (store_dir / "scratch").exists()
        assert offline_remotes.gh_calls() == []

    def test_canonical_approval_time_is_the_exact_commit_date(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        rec = _approved_run(store_dir)
        _store_approval_field(store_dir, rec.run_id, "approved_at", "2026-10-07T08:09:10+05:30")

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 0

        dates = subprocess.run(
            ["git", "log", "-1", "--format=%aI %cI", f"apprentice/{rec.run_id}"],
            cwd=offline_remotes.bare["no-magic-ai/no-magic"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert dates == ["2026-10-07T08:09:10+05:30"] * 2


class TestSubmitUsesTheCapturedSnapshot:
    def test_bundle_swapped_after_the_approval_check_is_not_published(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import apprentice.gates.review as review

        rec = _approved_run(store_dir)
        bundle = SessionStore(store_dir=store_dir).bundle_dir(rec.run_id)
        approved = {
            name: (bundle / name).read_bytes() for name in ("implementation.py", "scene.py")
        }
        real_check = review.require_approved_snapshot

        def check_then_swap(*args: Any, **kwargs: Any) -> Any:
            snapshot = real_check(*args, **kwargs)
            for name, data in approved.items():
                path = bundle / name
                path.chmod(0o644)
                path.write_bytes(data.replace(b"i", b"j").replace(b"c", b"d"))
            return snapshot

        monkeypatch.setattr(review, "require_approved_snapshot", check_then_swap)

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 0

        branch = f"apprentice/{rec.run_id}"
        assert (
            offline_remotes.blob("no-magic-ai/no-magic", branch, "02-alignment/microselection.py")
            == approved["implementation.py"]
        )
        assert (
            offline_remotes.blob(
                "no-magic-ai/no-magic-viz", branch, "scenes/scene_microselection.py"
            )
            == approved["scene.py"]
        )


class TestReviewGateRemediation:
    def test_run_that_did_not_complete_is_routed_to_a_rebuild(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = SessionStore(store_dir=store_dir)
        rec = store.create_run("selection", tier=2)
        store.fail_run(rec, {}, {}, 1.0, "provider down")

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        assert json.loads(capsys.readouterr().out)["remediation"] == (
            "apprentice build selection --tier 2"
        )

    def test_approval_of_another_bundle_is_routed_to_preview_and_reapproval(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = _approved_run(store_dir)
        _store_approval_field(store_dir, rec.run_id, "manifest_sha256", "0" * 64)
        capsys.readouterr()

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1

        remediation = json.loads(capsys.readouterr().out)["remediation"]
        assert f"apprentice preview --run-id {rec.run_id}" in remediation
        assert f"apprentice approve {rec.run_id}" in remediation


def _refused_without_effects(
    store_dir: Path,
    offline_remotes: OfflineRemotes,
    capsys: pytest.CaptureFixture[str],
    command: Callable[[], int],
) -> dict[str, Any]:
    before = _tree(store_dir)
    capsys.readouterr()

    assert command() == 1

    out: dict[str, Any] = json.loads(_last_json(capsys))
    assert _tree(store_dir) == before
    assert offline_remotes.gh_calls() == []
    assert offline_remotes.branches("no-magic-ai/no-magic") == ["main"]
    assert offline_remotes.branches("no-magic-ai/no-magic-viz") == ["main"]
    return out


class TestStoredApprovalShape:
    @pytest.mark.parametrize("approval", [5, ["approved"], "approved", True, None])
    def test_approval_that_is_not_an_object_is_refused_before_any_attempt(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        approval: object,
    ) -> None:
        rec = _approved_run(store_dir)
        _damage_record(store_dir, rec.run_id, lambda r: r.update(approval=approval))

        out = _refused_without_effects(
            store_dir,
            offline_remotes,
            capsys,
            lambda: _cmd_submit(_SubmitArgs("selection", rec.run_id)),
        )

        assert out["remediation"] == f"apprentice approve {rec.run_id}"

    @pytest.mark.parametrize(("run_tier", "tier"), [(2, 2.0), (1, True)])
    def test_approved_tier_that_is_not_an_integer_is_refused(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        run_tier: int,
        tier: object,
    ) -> None:
        # Each damaged tier compares equal to the sealed tier (2.0 == 2, True == 1).
        store = SessionStore(store_dir=store_dir)
        rec = store.complete_run(
            store.create_run("selection", tier=run_tier),
            session_state=dict(_STATE),
            budget_summary={},
            elapsed=1.0,
        )
        assert _cmd_approve(_ApproveArgs(rec.run_id)) == 0
        _damage_record(store_dir, rec.run_id, lambda r: r["approval"].update(tier=tier))

        out = _refused_without_effects(
            store_dir,
            offline_remotes,
            capsys,
            lambda: _cmd_submit(_SubmitArgs("selection", rec.run_id)),
        )

        assert f"apprentice approve {rec.run_id}" in out["remediation"]


_MALFORMED_GATE_RECORDS: list[object] = [
    "summary",
    {"gate_verdicts": "verdicts"},
    {"gate_verdicts": [5]},
    {"gate_verdicts": [{"verdict": "fail"}]},
    {"gate_verdicts": [{"verdict": "fail", "blocking": True, "gate_name": "lint"}]},
]


class TestMalformedGateRecords:
    @pytest.mark.parametrize("summary", _MALFORMED_GATE_RECORDS)
    @pytest.mark.parametrize("command", ["approve", "submit"])
    def test_malformed_gate_records_are_refused_with_the_rebuild_remediation(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        summary: object,
        command: str,
    ) -> None:
        rec = _approved_run(store_dir)
        _damage_record(store_dir, rec.run_id, lambda r: r.update(budget_summary=summary))

        def run() -> int:
            if command == "approve":
                return _cmd_approve(_ApproveArgs(rec.run_id, approver="other"))
            return _cmd_submit(_SubmitArgs("selection", rec.run_id))

        out = _refused_without_effects(store_dir, offline_remotes, capsys, run)

        assert out["remediation"] == "apprentice build selection --tier 2"


class TestStoredSubmissionShape:
    @pytest.mark.parametrize("submission", [None, 0, False, "", [], 5, "attempt", ["pending"]])
    @pytest.mark.parametrize("command", ["approve", "submit"])
    def test_submission_that_is_not_an_object_is_refused_before_any_attempt(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        submission: object,
        command: str,
    ) -> None:
        rec = _approved_run(store_dir)
        _damage_record(store_dir, rec.run_id, lambda r: r.update(submission=submission))

        def run() -> int:
            if command == "approve":
                return _cmd_approve(_ApproveArgs(rec.run_id, approver="other"))
            return _cmd_submit(_SubmitArgs("selection", rec.run_id))

        out = _refused_without_effects(store_dir, offline_remotes, capsys, run)

        assert out["submission"] == submission

    def test_nonempty_attempt_without_a_status_blocks_submit_before_publication(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rec = _approved_run(store_dir)
        _damage_record(store_dir, rec.run_id, lambda r: r.update(submission={"garbage": True}))

        out = _refused_without_effects(
            store_dir,
            offline_remotes,
            capsys,
            lambda: _cmd_submit(_SubmitArgs("selection", rec.run_id)),
        )

        assert out["submission"] == {"garbage": True}

    @pytest.mark.parametrize("stored", ["empty-object", "absent"])
    def test_empty_or_absent_submission_means_no_attempt_yet(
        self, store_dir: Path, offline_remotes: OfflineRemotes, stored: str
    ) -> None:
        rec = _approved_run(store_dir)
        if stored == "absent":
            _damage_record(store_dir, rec.run_id, lambda r: r.pop("submission"))
        else:
            _damage_record(store_dir, rec.run_id, lambda r: r.update(submission={}))

        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 0

        assert SessionStore(store_dir=store_dir).load(rec.run_id).submission["status"] == "complete"
        assert len(offline_remotes.gh_calls()) == 2


class TestUnknownPublicationOutcome:
    def test_core_push_timeout_is_recorded_partial_with_an_unknown_push(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        short_publish_deadline: int,
    ) -> None:
        from tests.conftest import release

        rec = _approved_run(store_dir)
        gate = tmp_path / "receive-gate"
        offline_remotes.hold_after_receive("no-magic-ai/no-magic", gate)
        capsys.readouterr()
        try:
            assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1
        finally:
            release(gate)

        printed = json.loads(_last_json(capsys))
        stored = SessionStore(store_dir=store_dir).load(rec.run_id).submission
        assert printed["status"] == stored["status"] == "partial"
        assert [(r["pushed"], r["pr_url"]) for r in stored["repositories"]] == [
            (None, ""),
            (False, ""),
        ]
        assert f"apprentice/{rec.run_id}" in offline_remotes.branches("no-magic-ai/no-magic")
        assert _cmd_submit(_SubmitArgs("selection", rec.run_id)) == 1
        assert offline_remotes.gh_calls() == []


@pytest.mark.usefixtures("judged_execution")
class TestImplementationRetries:
    @pytest.mark.parametrize(
        ("retries", "failing", "drafts"),
        [(1, True, 1), (2, True, 2), (2, False, 1)],
        ids=["one-failing", "two-failing", "passing-first-draft"],
    )
    def test_implementation_is_drafted_at_most_the_configured_number_of_times(
        self,
        tmp_path: Path,
        store_dir: Path,
        retries: int,
        failing: bool,
        drafts: int,
    ) -> None:
        fixture = ResponsesFixture(outputs=fixture_outputs(failing_implementation=failing))
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
                max_tokens_per_stage=100_000,
                max_implementation_retries=retries,
            )

            code = main(["--config", str(config), "build", "selection"])

        record = _only_record(store_dir)
        assert (code, record.status) == ((1, "failed") if failing else (0, "completed"))
        assert _generated_roles(fixture).count("drafter") == drafts


def _stop_at(monkeypatch: pytest.MonkeyPatch, method: str, when: str) -> None:
    """Deliver a real SIGTERM to this process at the start or end of `SessionStore.<method>`."""
    import signal as signals

    original = getattr(SessionStore, method)

    def stopped(self: SessionStore, *args: Any, **kwargs: Any) -> Any:
        if when == "before":
            os.kill(os.getpid(), signals.SIGTERM)
        result = original(self, *args, **kwargs)
        if when == "after":
            os.kill(os.getpid(), signals.SIGTERM)
        return result

    monkeypatch.setattr(SessionStore, method, stopped)


def _ledger_cycles(store_dir: Path) -> list[tuple[str, str, str | None]]:
    """Cycle IDs, states and outcomes after a fresh process-style recovery of the ledger."""
    import sqlite3

    from apprentice.controls.authority import Authority
    from apprentice.controls.footprint import Footprint

    authority = Authority.open(store_dir, Footprint(existing=()))
    authority.status()
    authority.close()
    conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
    try:
        return conn.execute(
            "SELECT cycle_id, state, outcome FROM cycles ORDER BY admitted_at"
        ).fetchall()
    finally:
        conn.close()


# A completing build runs three parallel 15,000-token artifact roles; with the default 20,000-token
# stage the role admitted last gets the residual adaptive cap and its truncated output halts the build
# before `complete_run`. Completing cases therefore get an explicit adequate stage.
_COMPLETING_STAGE = {"max_tokens_per_stage": 100_000}


@pytest.mark.usefixtures("judged_execution")
class TestOperatorStopWhileRecordingTheEnd:
    @pytest.mark.parametrize(
        ("method", "when", "failing", "expected", "toy_config"),
        [
            ("complete_run", "before", False, "completed", _COMPLETING_STAGE),
            ("complete_run", "after", False, "completed", _COMPLETING_STAGE),
            ("fail_run", "before", True, "failed", {}),
            ("fail_run", "after", True, "failed", {}),
        ],
        ids=[
            "complete_run-before-False-completed",
            "complete_run-after-False-completed",
            "fail_run-before-True-failed",
            "fail_run-after-True-failed",
        ],
        indirect=["toy_config"],
    )
    def test_stop_during_the_end_record_keeps_the_outcome_reached_and_never_leaves_it_owner_lost(
        self,
        store_dir: Path,
        toy_config: tuple[Path, ResponsesFixture],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        method: str,
        when: str,
        failing: bool,
        expected: str,
    ) -> None:
        config, fixture = toy_config
        fixture.outputs = fixture_outputs(failing_implementation=failing)
        _stop_at(monkeypatch, method, when)

        code = main(["--config", str(config), "build", "selection"])

        reported = json.loads(_last_json(capsys))
        record = _only_record(store_dir)
        assert code == 143
        assert record.status == expected
        assert _ledger_cycles(store_dir) == [(reported["cycle_id"], "terminal", expected)]
        assert (reported["outcome"], reported["run_id"]) == (expected, record.run_id)
        if expected == "completed":
            assert SessionStore(store_dir=store_dir).load_bundle(record).artifacts

    def test_stop_during_the_model_work_still_cancels_the_build(
        self,
        store_dir: Path,
        toy_config: tuple[Path, ResponsesFixture],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import signal as signals

        config, fixture = toy_config
        fixture.outputs = fixture_outputs()
        respond = fixture.respond

        def stop_on_first_generation(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if path == "/v1/responses":
                fixture.respond = respond  # type: ignore[method-assign]
                os.kill(os.getpid(), signals.SIGTERM)
            return respond(path, body)

        fixture.respond = stop_on_first_generation  # type: ignore[method-assign]

        code = main(["--config", str(config), "build", "selection"])

        reported = json.loads(_last_json(capsys))
        record = _only_record(store_dir)
        assert code == 143
        assert record.status == "failed" and record.error == "cancelled"
        assert _ledger_cycles(store_dir) == [(reported["cycle_id"], "terminal", "cancelled")]
        assert (reported["outcome"], reported["run_id"]) == ("cancelled", record.run_id)


def _generation_states(store_dir: Path) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
    try:
        rows = conn.execute("SELECT state FROM entries WHERE operation = 'generate'").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


@pytest.mark.usefixtures("judged_execution")
class TestRunRecordThatCannotBeSaved:
    """The record store root becomes read-only (a real EACCES at the record writer)
    while the control ledger under `controls/` stays writable."""

    def test_stop_during_the_model_work_still_ends_the_cycle_cancelled(
        self,
        store_dir: Path,
        toy_config: tuple[Path, ResponsesFixture],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import signal as signals

        config, fixture = toy_config
        fixture.outputs = fixture_outputs()
        assert main(["--config", str(config), "status"]) == 0
        capsys.readouterr()
        respond = fixture.respond
        original = store_dir.stat().st_mode & 0o777

        def stop_read_only(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if path == "/v1/responses":
                fixture.respond = respond  # type: ignore[method-assign]
                store_dir.chmod(0o500)
                os.kill(os.getpid(), signals.SIGTERM)
            return respond(path, body)

        fixture.respond = stop_read_only  # type: ignore[method-assign]
        try:
            code = main(["--config", str(config), "build", "selection"])
        finally:
            store_dir.chmod(original)

        reported = json.loads(_last_json(capsys))
        record = _only_record(store_dir)
        assert code == 128 + signals.SIGTERM
        assert (reported["outcome"], reported["run_id"]) == ("cancelled", record.run_id)
        assert str(store_dir) in reported["error"]
        assert record.status == "in_progress"
        assert _ledger_cycles(store_dir) == [(reported["cycle_id"], "terminal", "cancelled")]
        assert _generation_states(store_dir) == ["unknown"]

    @pytest.mark.parametrize(
        ("failing", "toy_config"),
        [(True, {}), (False, _COMPLETING_STAGE)],
        ids=["failed-build", "completed-build"],
        indirect=["toy_config"],
    )
    def test_a_build_whose_record_cannot_be_saved_ends_its_cycle_failed_once(
        self,
        store_dir: Path,
        toy_config: tuple[Path, ResponsesFixture],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        failing: bool,
    ) -> None:
        config, fixture = toy_config
        fixture.outputs = fixture_outputs(failing_implementation=failing)
        assert main(["--config", str(config), "status"]) == 0
        capsys.readouterr()
        original = store_dir.stat().st_mode & 0o777
        write = SessionStore._write
        attempted: list[str] = []

        def write_to_a_read_only_store(self: SessionStore, record: RunRecord) -> None:
            attempted.append(record.status)
            store_dir.chmod(0o500)
            write(self, record)

        monkeypatch.setattr(SessionStore, "_write", write_to_a_read_only_store)
        try:
            code = main(["--config", str(config), "build", "selection"])
        finally:
            store_dir.chmod(original)

        out = json.loads(_last_json(capsys))
        record = _only_record(store_dir)
        assert code == 1
        # A completed build's end is refused first, then its failed end.
        assert attempted == (["failed"] if failing else ["completed", "failed"])
        assert (out["outcome"], out["run_id"]) == ("failed", record.run_id)
        assert str(store_dir) in out["error"]
        assert record.status == "in_progress"
        assert _ledger_cycles(store_dir) == [(out["accounting"]["cycle_id"], "terminal", "failed")]

    def test_a_run_that_cannot_be_created_ends_its_cycle_and_is_reported(
        self,
        store_dir: Path,
        toy_config: tuple[Path, ResponsesFixture],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config, fixture = toy_config
        assert main(["--config", str(config), "status"]) == 0
        original = store_dir.stat().st_mode & 0o777
        capsys.readouterr()
        store_dir.chmod(0o500)
        try:
            code = main(["--config", str(config), "build", "selection"])
        finally:
            store_dir.chmod(original)

        captured = capsys.readouterr()
        out = json.loads(captured.out[captured.out.rfind("\n{") + 1 :])
        ((_cycle_id, state, outcome),) = _ledger_cycles(store_dir)
        assert code == 1
        assert str(store_dir) in out["error"]
        assert (state, outcome) == ("terminal", "failed")
        assert "Traceback" not in captured.err
        assert fixture.generations() == []
        assert SessionStore(store_dir=store_dir).list_runs(limit=None) == []

    @pytest.mark.parametrize(
        ("failing", "writable", "toy_config"),
        [(True, False, {}), (False, False, _COMPLETING_STAGE), (True, True, {})],
        ids=["failed-build", "completed-build", "healthy-store"],
        indirect=["toy_config"],
    )
    def test_stop_while_the_end_is_recorded_reports_an_unsaved_record(
        self,
        store_dir: Path,
        toy_config: tuple[Path, ResponsesFixture],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        failing: bool,
        writable: bool,
    ) -> None:
        import signal as signals

        config, fixture = toy_config
        fixture.outputs = fixture_outputs(failing_implementation=failing)
        assert main(["--config", str(config), "status"]) == 0
        capsys.readouterr()
        original = store_dir.stat().st_mode & 0o777
        write = SessionStore._write
        stopped: list[bool] = []
        attempted: list[str] = []

        def stop_then_write(self: SessionStore, record: RunRecord) -> None:
            attempted.append(record.status)
            # The operator's stop arrives while the run's end is being recorded.
            if not stopped:
                stopped.append(True)
                if not writable:
                    store_dir.chmod(0o500)
                os.kill(os.getpid(), signals.SIGTERM)
            write(self, record)

        monkeypatch.setattr(SessionStore, "_write", stop_then_write)
        try:
            code = main(["--config", str(config), "build", "selection"])
        finally:
            store_dir.chmod(original)

        reported = json.loads(_last_json(capsys))
        record = _only_record(store_dir)
        expected = "failed" if failing or not writable else "completed"
        assert code == 128 + signals.SIGTERM
        # A completed build's end is refused first, then its failed end.
        assert attempted == (["failed"] if failing else ["completed", "failed"])
        assert (reported["outcome"], reported["run_id"]) == (expected, record.run_id)
        assert _ledger_cycles(store_dir) == [(reported["cycle_id"], "terminal", expected)]
        if writable:
            assert record.status == expected
            assert str(store_dir) not in reported["error"]
        else:
            assert record.status == "in_progress"
            assert str(store_dir) in reported["error"]


class TestWritesIntoAStoreThatCannotBeWritten:
    def test_approving_a_sealed_run_in_a_search_only_store_is_refused_typed(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rec = _make_completed_run(store_dir)
        record = store_dir / f"{rec.run_id}.json"
        data, original = record.read_bytes(), store_dir.stat().st_mode & 0o777
        store_dir.chmod(0o100)
        try:
            code = _cmd_approve(_ApproveArgs(rec.run_id))
        finally:
            store_dir.chmod(original)

        out = json.loads(_last_json(capsys))
        assert code == 1
        assert out["run_id"] == rec.run_id
        assert str(record) in out["error"]
        assert record.read_bytes() == data

    def test_submitting_from_a_search_only_store_reserves_and_publishes_nothing(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rec = _approved_run(store_dir)
        record = store_dir / f"{rec.run_id}.json"
        data, original = record.read_bytes(), store_dir.stat().st_mode & 0o777
        capsys.readouterr()
        store_dir.chmod(0o100)
        try:
            code = _cmd_submit(_SubmitArgs("selection", rec.run_id))
        finally:
            store_dir.chmod(original)

        out = json.loads(_last_json(capsys))
        assert code == 1
        assert out["run_id"] == rec.run_id
        assert str(store_dir) in out["error"]
        assert record.read_bytes() == data
        assert offline_remotes.gh_calls() == []
