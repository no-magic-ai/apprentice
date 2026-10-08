"""Same-run coordination of `approve` and `submit` across real processes.

Each contender runs `_cmd_submit` or `_cmd_approve` in its own interpreter
against the same store and offline remotes. A contender stops at a named
point by writing to one pipe and blocking on another until it is released or
killed, so every interleaving below is forced rather than timed. A contender
stopped at "lock-wait" has found the run's lock held by another process.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import apprentice.agents.packaging as packaging
from apprentice.cli import _cmd_approve, _cmd_submit
from apprentice.core.session_store import RunRecord, SessionStore

if TYPE_CHECKING:
    from tests.conftest import OfflineRemotes

_DRIVER = r"""
import os, sys
from pathlib import Path
from types import SimpleNamespace

import apprentice.agents.packaging as packaging
import apprentice.core.session_store as session_store
from apprentice import cli

store_dir, point, reached_fd, go_fd, command, run_id, approver = sys.argv[1:]
session_store.default_store_dir = lambda: Path(store_dir)
paused = []


def pause():
    if paused:
        return
    paused.append(point)
    os.write(int(reached_fd), b"1")
    if os.read(int(go_fd), 1) != b"1":
        raise SystemExit("released without go")


def wrap(owner, name, pause_after):
    original = getattr(owner, name)

    def wrapped(*args, **kwargs):
        if not pause_after:
            pause()
        result = original(*args, **kwargs)
        if pause_after:
            pause()
        return result

    setattr(owner, name, wrapped)


if point == "lock":  # before taking the run's record lock
    wrap(session_store.SessionStore, "record_lock", pause_after=False)
elif point == "lock-wait":  # only once the record lock is found held by another process
    import fcntl

    original_flock = fcntl.flock

    def flock(fd, operation):
        try:
            original_flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            pause()
            original_flock(fd, operation)

    fcntl.flock = flock
elif point == "claim":  # holding the lock with fresh scratch, before the pending save
    wrap(session_store.SessionStore, "allocate_work_root", pause_after=True)
elif point == "effects":  # pending saved and lock released, before any clone
    wrap(packaging, "submit_snapshot", pause_after=False)
elif point == "pull-request":  # both branches pushed, before the first `gh pr create`
    original_run = packaging._run

    def run(args, *rest, **kwargs):
        if args[0] == "gh":
            pause()
        return original_run(args, *rest, **kwargs)

    packaging._run = run
elif point == "finish-wait":  # effects done, before the terminal update; reports a held run lock
    import fcntl

    finishing = []
    original_finish = cli._finish_submission

    def finish(*args, **kwargs):
        pause()
        finishing.append(True)
        return original_finish(*args, **kwargs)

    cli._finish_submission = finish
    original_flock = fcntl.flock

    def flock(fd, operation):
        if finishing:
            try:
                original_flock(fd, operation | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                os.write(int(reached_fd), b"2")
        original_flock(fd, operation)

    fcntl.flock = flock

if command == "submit":
    code = cli._cmd_submit(SimpleNamespace(algorithm="selection", run_id=run_id, tier=None))
else:
    code = cli._cmd_approve(SimpleNamespace(run_id=run_id, approver=approver))
sys.exit(code)
"""


class _Contender:
    """A CLI command in a fresh interpreter that stops once at `point`."""

    def __init__(
        self, store_dir: Path, point: str, command: str, run_id: str, approver: str = ""
    ) -> None:
        self._reached, reached_w = os.pipe()
        go_r, self._go = os.pipe()
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _DRIVER,
                str(store_dir),
                point,
                str(reached_w),
                str(go_r),
                command,
                run_id,
                approver,
            ],
            pass_fds=(reached_w, go_r),
            env={**os.environ, "LITELLM_MODE": "PRODUCTION"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(reached_w)
        os.close(go_r)

    def reached(self) -> None:
        if os.read(self._reached, 1) != b"1":
            code, out, err = self._collect()
            pytest.fail(f"contender exited ({code}) before its pause point: {out}{err}")

    def release(self) -> None:
        os.write(self._go, b"1")

    def signal(self) -> bytes:
        """The contender's next signal byte, or b"" once it has exited."""
        return os.read(self._reached, 1)

    def finish(self) -> tuple[int, dict[str, Any]]:
        code, out, _ = self._collect()
        return code, json.loads(out[out.rfind("\n{") + 1 :] if "\n{" in out else out)

    def kill(self) -> None:
        self.process.kill()
        self._collect()

    def _collect(self) -> tuple[int, str, str]:
        out, err = self.process.communicate()
        os.close(self._reached)
        os.close(self._go)
        return self.process.returncode, out, err


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "sessions"
    # cycles binds the name at import: patched there first (importing it before the store's
    # default is replaced), a first import during this test cannot keep this test's store as
    # every later test's default.
    monkeypatch.setattr("apprentice.core.cycles.default_store_dir", lambda: directory)
    monkeypatch.setattr("apprentice.core.session_store.default_store_dir", lambda: directory)
    return directory


def _approved_run(store_dir: Path, approver: str = "tester") -> RunRecord:
    store = SessionStore(store_dir=store_dir)
    record = store.create_run("selection", tier=2)
    state = {
        "generated_code": "print('hi')\n",
        "manim_scene_code": "print('scene')\n",
        "anki_deck_content": "front,back\n",
    }
    store.complete_run(record, session_state=state, budget_summary={}, elapsed=1.0)
    assert _cmd_approve(SimpleNamespace(run_id=record.run_id, approver=approver)) == 0
    return store.load(record.run_id)


def _submit(run_id: str) -> int:
    return _cmd_submit(SimpleNamespace(algorithm="selection", run_id=run_id, tier=None))


def _last_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    return dict(json.loads(out[out.rfind("\n{") + 1 :] if "\n{" in out else out))


def _branches(remotes: OfflineRemotes) -> dict[str, list[str]]:
    return {repository: remotes.branches(repository) for repository in remotes.bare}


def _approved_by_in_commits(remotes: OfflineRemotes, branch: str) -> set[str]:
    messages = [
        subprocess.run(
            ["git", "log", "-1", "--format=%B", branch],
            cwd=bare,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for bare in remotes.bare.values()
    ]
    return {
        line.removeprefix("Approved-By: ")
        for message in messages
        for line in message.splitlines()
        if line.startswith("Approved-By: ")
    }


def _scratch(store_dir: Path) -> list[Path]:
    scratch = store_dir / "scratch"
    return sorted(scratch.iterdir()) if scratch.exists() else []


def _tree(root: Path) -> dict[str, tuple[bytes | None, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes() if path.is_file() and not path.is_symlink() else None,
            path.lstat().st_mode,
        )
        for path in sorted(root.rglob("*"))
    }


def _lock_path(store_dir: Path, run_id: str) -> Path:
    return store_dir / "runs" / f"{run_id}.lock"


class TestRecordLock:
    def test_a_held_lock_excludes_every_other_holder_of_the_run(self, store_dir: Path) -> None:
        store = SessionStore(store_dir=store_dir)
        record = _approved_run(store_dir)
        path = _lock_path(store_dir, record.run_id)
        other = os.open(path, os.O_RDWR)
        try:
            with store.record_lock(record.run_id), pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)

    def test_lock_file_is_one_stable_empty_private_file(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir)
        path = _lock_path(store_dir, record.run_id)
        before = path.lstat()

        assert _submit(record.run_id) == 0

        after = path.lstat()
        assert (after.st_ino, after.st_nlink, after.st_size) == (before.st_ino, 1, 0)
        assert stat.S_ISREG(after.st_mode) and stat.S_IMODE(after.st_mode) == 0o600

    def test_refused_valid_run_gains_only_its_lock_file(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        store = SessionStore(store_dir=store_dir)
        record = store.create_run("selection", tier=2)
        store.complete_run(
            record, session_state={"generated_code": "x = 1\n"}, budget_summary={}, elapsed=1.0
        )
        before = _tree(store_dir)

        assert _submit(record.run_id) == 1  # never approved

        after = _tree(store_dir)
        lock = f"runs/{record.run_id}.lock"
        assert set(after) - set(before) == {lock}
        assert {k: v for k, v in after.items() if k != lock} == before
        assert after[lock] == (b"", stat.S_IFREG | 0o600)
        assert offline_remotes.gh_calls() == []

    @pytest.mark.parametrize("run_id", ["../../outside", "selection-20990101T000000Z-" + "0" * 32])
    def test_invalid_or_missing_run_creates_no_lock(self, store_dir: Path, run_id: str) -> None:
        _approved_run(store_dir)
        before = _tree(store_dir)

        assert _submit(run_id) == 1
        assert _cmd_approve(SimpleNamespace(run_id=run_id, approver="tester")) == 1

        assert _tree(store_dir) == before

    @pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink"])
    def test_lock_path_that_is_not_a_private_plain_file_is_refused(
        self,
        store_dir: Path,
        tmp_path: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        kind: str,
    ) -> None:
        record = _approved_run(store_dir)
        path = _lock_path(store_dir, record.run_id)
        path.unlink()
        outside = tmp_path / "outside.lock"
        outside.write_bytes(b"")
        if kind == "symlink":
            path.symlink_to(outside)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            os.link(outside, path)
        before = _tree(store_dir)
        capsys.readouterr()

        assert _submit(record.run_id) == 1
        submit_error = _last_json(capsys)["error"]
        assert _cmd_approve(SimpleNamespace(run_id=record.run_id, approver="other")) == 1
        approve_error = _last_json(capsys)["error"]

        expected = "refusing to follow symlink" if kind == "symlink" else "single-link regular"
        assert expected in submit_error and expected in approve_error
        assert _tree(store_dir) == before
        assert outside.read_bytes() == b""
        assert offline_remotes.gh_calls() == []
        assert _branches(offline_remotes) == {r: ["main"] for r in offline_remotes.bare}


class TestConcurrentSubmit:
    def test_submit_waiting_for_the_claim_is_refused_and_one_forest_is_published(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir)
        first = _Contender(store_dir, "claim", "submit", record.run_id)
        first.reached()
        second = _Contender(store_dir, "lock-wait", "submit", record.run_id)
        second.reached()
        second.release()
        first.release()

        first_code, first_out = first.finish()
        second_code, second_out = second.finish()

        assert (first_code, second_code) == (0, 1)
        assert "already has a submission attempt" in second_out["error"]
        branch = f"apprentice/{record.run_id}"
        assert _branches(offline_remotes) == {r: [branch, "main"] for r in offline_remotes.bare}
        assert len(offline_remotes.gh_calls()) == 2
        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        assert stored["status"] == "complete"
        assert stored["workspace"] == first_out["workspace"]
        assert [p.name for p in _scratch(store_dir)] == [Path(first_out["workspace"]).name]

    def test_submit_during_the_effects_of_another_is_refused_without_effects(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        record = _approved_run(store_dir)
        first = _Contender(store_dir, "effects", "submit", record.run_id)
        first.reached()
        capsys.readouterr()

        assert _submit(record.run_id) == 1
        refused = _last_json(capsys)
        assert refused["submission"]["status"] == "pending"
        assert offline_remotes.gh_calls() == []
        assert _branches(offline_remotes) == {r: ["main"] for r in offline_remotes.bare}

        first.release()
        assert first.finish()[0] == 0
        assert len(offline_remotes.gh_calls()) == 2

    def test_killed_contender_waiting_for_the_lock_writes_nothing(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir)
        first = _Contender(store_dir, "claim", "submit", record.run_id)
        first.reached()
        second = _Contender(store_dir, "lock-wait", "submit", record.run_id)
        second.reached()
        second.kill()
        first.release()

        code, out = first.finish()

        assert code == 0
        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        assert (stored["status"], stored["workspace"]) == ("complete", out["workspace"])
        assert len(_scratch(store_dir)) == 1
        assert len(offline_remotes.gh_calls()) == 2


class TestInterruptedSubmit:
    def test_kill_while_claiming_leaves_no_attempt_and_a_rerun_publishes_once(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir)
        killed = _Contender(store_dir, "claim", "submit", record.run_id)
        killed.reached()
        killed.kill()

        assert SessionStore(store_dir=store_dir).load(record.run_id).submission == {}
        (orphan,) = _scratch(store_dir)
        assert list(orphan.iterdir()) == []
        assert offline_remotes.gh_calls() == []

        assert _submit(record.run_id) == 0
        assert SessionStore(store_dir=store_dir).load(record.run_id).submission["status"] == (
            "complete"
        )
        assert len(offline_remotes.gh_calls()) == 2

    @pytest.mark.parametrize(("point", "pushed"), [("effects", False), ("pull-request", True)])
    def test_kill_after_the_claim_keeps_it_pending_and_refuses_reruns(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        point: str,
        pushed: bool,
    ) -> None:
        record = _approved_run(store_dir)
        killed = _Contender(store_dir, point, "submit", record.run_id)
        killed.reached()
        killed.kill()

        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        assert (stored["status"], stored["repositories"]) == ("pending", [])
        branch = f"apprentice/{record.run_id}"
        expected = [branch, "main"] if pushed else ["main"]
        assert _branches(offline_remotes) == {r: expected for r in offline_remotes.bare}
        refs = _branches(offline_remotes)
        capsys.readouterr()

        assert _submit(record.run_id) == 1

        assert _last_json(capsys)["submission"] == stored
        assert _branches(offline_remotes) == refs
        assert offline_remotes.gh_calls() == []


class TestApproveAgainstSubmit:
    def test_approval_recorded_before_the_claim_is_the_one_published(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir, approver="first-reviewer")
        submitter = _Contender(store_dir, "lock", "submit", record.run_id)
        submitter.reached()

        assert _cmd_approve(SimpleNamespace(run_id=record.run_id, approver="second-reviewer")) == 0
        submitter.release()

        assert submitter.finish()[0] == 0
        assert _approved_by_in_commits(offline_remotes, f"apprentice/{record.run_id}") == {
            "second-reviewer"
        }
        assert all("second-reviewer" in " ".join(call) for call in offline_remotes.gh_calls())

    def test_approval_after_the_claim_is_refused_and_left_unchanged(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        record = _approved_run(store_dir, approver="first-reviewer")
        submitter = _Contender(store_dir, "effects", "submit", record.run_id)
        submitter.reached()
        approval = SessionStore(store_dir=store_dir).load(record.run_id).approval
        capsys.readouterr()

        assert _cmd_approve(SimpleNamespace(run_id=record.run_id, approver="late-reviewer")) == 1

        assert "approval can no longer change" in _last_json(capsys)["error"]
        assert SessionStore(store_dir=store_dir).load(record.run_id).approval == approval
        submitter.release()
        assert submitter.finish()[0] == 0
        assert _approved_by_in_commits(offline_remotes, f"apprentice/{record.run_id}") == {
            "first-reviewer"
        }
        assert SessionStore(store_dir=store_dir).load(record.run_id).approval == approval

    def test_approval_waiting_for_the_claim_is_refused(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir, approver="first-reviewer")
        submitter = _Contender(store_dir, "claim", "submit", record.run_id)
        submitter.reached()
        approver = _Contender(store_dir, "lock-wait", "approve", record.run_id, "late-reviewer")
        approver.reached()
        approver.release()
        submitter.release()

        assert submitter.finish()[0] == 0
        code, out = approver.finish()

        assert code == 1
        assert "approval can no longer change" in out["error"]
        stored = SessionStore(store_dir=store_dir).load(record.run_id)
        assert stored.approval["approved_by"] == "first-reviewer"
        assert _approved_by_in_commits(offline_remotes, f"apprentice/{record.run_id}") == {
            "first-reviewer"
        }


class TestTerminalRecord:
    @pytest.mark.parametrize("change", ["deleted", "replaced"])
    @pytest.mark.parametrize("fail_on", [None, "2"])
    def test_out_of_band_change_is_not_overwritten_and_known_effects_are_reported(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        change: str,
        fail_on: str | None,
    ) -> None:
        record = _approved_run(store_dir)
        path = store_dir / f"{record.run_id}.json"
        replacement = b""
        real_submit = packaging.submit_snapshot

        def submit_then_change(*args: Any) -> Any:
            nonlocal replacement
            try:
                return real_submit(*args)
            finally:
                if change == "deleted":
                    path.unlink()
                else:
                    data = json.loads(path.read_bytes())
                    data["submission"] = {"status": "pending", "note": "another writer"}
                    replacement = json.dumps(data).encode()
                    path.write_bytes(replacement)

        monkeypatch.setattr(packaging, "submit_snapshot", submit_then_change)
        if fail_on:
            monkeypatch.setenv("OFFLINE_GH_FAIL_ON", fail_on)
        capsys.readouterr()

        assert _submit(record.run_id) == 1

        out = _last_json(capsys)
        assert "was not updated" in out["error"]
        assert out["reserved"]["status"] == "pending"
        assert out["outcome"]["status"] == ("partial" if fail_on else "complete")
        assert [r["pr_url"] for r in out["outcome"]["repositories"]] == (
            ["https://github.com/no-magic-ai/no-magic/pull/offline-1", ""]
            if fail_on
            else [
                "https://github.com/no-magic-ai/no-magic/pull/offline-1",
                "https://github.com/no-magic-ai/no-magic-viz/pull/offline-2",
            ]
        )
        if change == "deleted":
            assert out["stored"] is None
            assert not path.exists()
        else:
            assert out["stored"] == {"status": "pending", "note": "another writer"}
            assert path.read_bytes() == replacement

    @pytest.mark.parametrize(
        "damage", ["missing-required", "not-object", "unreadable", "directory"]
    )
    @pytest.mark.parametrize("fail_on", [None, "2"])
    def test_unreadable_or_invalid_record_after_effects_still_reports_the_known_outcome(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        damage: str,
        fail_on: str | None,
    ) -> None:
        record = _approved_run(store_dir)
        path = store_dir / f"{record.run_id}.json"
        damaged = b""
        real_submit = packaging.submit_snapshot

        def submit_then_damage(*args: Any) -> Any:
            nonlocal damaged
            try:
                return real_submit(*args)
            finally:
                if damage == "missing-required":
                    data = json.loads(path.read_bytes())
                    del data["started_at"]
                    damaged = json.dumps(data).encode()
                    path.write_bytes(damaged)
                elif damage == "not-object":
                    damaged = b"[1, 2]"
                    path.write_bytes(damaged)
                elif damage == "unreadable":
                    path.chmod(0)
                else:
                    path.unlink()
                    path.mkdir()

        monkeypatch.setattr(packaging, "submit_snapshot", submit_then_damage)
        if fail_on:
            monkeypatch.setenv("OFFLINE_GH_FAIL_ON", fail_on)
        capsys.readouterr()
        try:
            assert _submit(record.run_id) == 1
        finally:
            if damage == "unreadable":
                path.chmod(0o644)

        out = _last_json(capsys)
        assert "was not updated" in out["error"]
        assert out["stored"] is None
        assert out["outcome"]["status"] == ("partial" if fail_on else "complete")
        core_pr = "https://github.com/no-magic-ai/no-magic/pull/offline-1"
        viz_pr = "https://github.com/no-magic-ai/no-magic-viz/pull/offline-2"
        assert [(r["pushed"], r["pr_url"]) for r in out["outcome"]["repositories"]] == (
            [(True, core_pr), (True, "")] if fail_on else [(True, core_pr), (True, viz_pr)]
        )
        if damage == "directory":
            assert path.is_dir()
        elif damage != "unreadable":
            assert path.read_bytes() == damaged

    def test_failed_outcome_save_reports_the_reserved_attempt_still_on_disk(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        record = _approved_run(store_dir)
        path = store_dir / f"{record.run_id}.json"
        real_submit = packaging.submit_snapshot

        def submit_then_freeze_store(*args: Any) -> Any:
            try:
                return real_submit(*args)
            finally:
                store_dir.chmod(0o555)

        monkeypatch.setattr(packaging, "submit_snapshot", submit_then_freeze_store)
        capsys.readouterr()
        try:
            assert _submit(record.run_id) == 1
        finally:
            store_dir.chmod(0o755)

        out = _last_json(capsys)
        on_disk = json.loads(path.read_bytes())["submission"]
        assert "saving the outcome failed" in out["error"]
        assert out["stored"] == out["reserved"] == on_disk
        assert on_disk["status"] == "pending"
        assert out["outcome"]["status"] == "complete"
        assert len(offline_remotes.gh_calls()) == 2

    def test_terminal_update_waits_for_the_run_lock_and_keeps_the_holders_change(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir)
        store = SessionStore(store_dir=store_dir)
        path = store_dir / f"{record.run_id}.json"
        submitter = _Contender(store_dir, "finish-wait", "submit", record.run_id)
        submitter.reached()  # both repositories published; terminal update not started

        with store.record_lock(record.run_id):
            submitter.release()
            found_lock_held = submitter.signal() == b"2"
            latest = store.load(record.run_id)
            latest.submission = {**latest.submission, "note": "holder transition"}
            store.save(latest)
            transition = path.read_bytes()
        code, out = submitter.finish()

        assert code == 1
        assert path.read_bytes() == transition
        assert out["stored"] == json.loads(transition)["submission"]
        assert out["outcome"]["status"] == "complete"
        assert [(r["pushed"], r["pr_url"]) for r in out["outcome"]["repositories"]] == [
            (True, "https://github.com/no-magic-ai/no-magic/pull/offline-1"),
            (True, "https://github.com/no-magic-ai/no-magic-viz/pull/offline-2"),
        ]
        assert found_lock_held
        assert len(offline_remotes.gh_calls()) == 2
