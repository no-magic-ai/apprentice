"""Same-run coordination of `approve` and `submit` across real processes.

Each contender runs the submit command (`tests.conftest.run_submit`) or `_cmd_approve` in its own interpreter
against the same store and offline remotes. A contender stops at a named
point by writing to one pipe and blocking on another until it is released or
killed, so every interleaving below is forced rather than timed. A contender
stopped at "lock-wait" has found the run's lock held by another process.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import apprentice.agents.packaging as packaging
from apprentice.cli import _cmd_approve, main
from apprentice.controls.authority import Authority
from apprentice.controls.footprint import Footprint
from apprentice.core.session_store import RunRecord, SessionStore
from tests.conftest import run_submit

if TYPE_CHECKING:
    from tests.conftest import OfflineRemotes

_DRIVER = r"""
import os, sys
from pathlib import Path
from types import SimpleNamespace

import apprentice.agents.packaging as packaging
import apprentice.core.session_store as session_store
from apprentice import cli

from tests.conftest import apply_offline_repository_urls

store_dir, point, reached_fd, go_fd, command, run_id, approver = sys.argv[1:]
session_store.default_store_dir = lambda: Path(store_dir)
apply_offline_repository_urls()
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
elif point == "attempt-end":  # every effect done, before the attempt's end is committed
    from apprentice.controls.authority import Authority

    wrap(Authority, "finish_publication", pause_after=False)
elif point == "cycle-end":  # attempt ended, before the cycle's terminal state is committed
    from apprentice.controls.authority import Cycle

    wrap(Cycle, "finish", pause_after=False)
elif point == "effects":  # pending saved and lock released, before any clone
    wrap(packaging, "submit_snapshot", pause_after=False)
elif point == "pull-request":  # both branches pushed, before the first `gh pr create`
    original_run = packaging._run

    def run(args, *rest, **kwargs):
        if args[0] == "gh":
            pause()
        return original_run(args, *rest, **kwargs)

    packaging._run = run
elif point == "outcome-save":  # effects and ends done; the store turns read-only before saving
    original_save_outcome = cli._finish_submission

    def save_outcome(store, *args, **kwargs):
        pause()
        store.store_dir.chmod(0o555)
        return original_save_outcome(store, *args, **kwargs)

    cli._finish_submission = save_outcome
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
    from tests.conftest import run_submit

    code = run_submit(SimpleNamespace(algorithm="selection", run_id=run_id, tier=None))
elif command == "submit-cli":  # the real entry point, with its operator signal handling
    from tests.conftest import submit_test_config

    config = str(submit_test_config())
    code = cli.main(["--config", config, "submit", "selection", "--run-id", run_id])
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

    def terminate(self) -> tuple[int, str]:
        """Send SIGTERM (an operator's stop request), then release; returns the exit code and stdout.

        The release follows the signal, so a stop that is deferred past this
        point lets the command run on and end instead of waiting forever.
        """
        self.process.terminate()
        with contextlib.suppress(BrokenPipeError):  # the command already ended
            os.write(self._go, b"1")
        code, out, _ = self._collect()
        return code, out

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
    return run_submit(SimpleNamespace(algorithm="selection", run_id=run_id, tier=None))


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

    @pytest.mark.parametrize("stop", ["SIGKILL", "SIGTERM"])
    def test_first_retry_after_a_stop_before_any_write_publishes_the_same_manifest_once(
        self, store_dir: Path, offline_remotes: OfflineRemotes, stop: str
    ) -> None:
        # The pending claim is saved and nothing was written; the very next
        # command is the retry (no status or other recovery command first).
        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        if stop == "SIGKILL":
            stopped = _Contender(store_dir, "effects", "submit", record.run_id)
            stopped.reached()
            stopped.kill()
            exit_code = -signal.SIGKILL
        else:
            stopped = _Contender(store_dir, "effects", "submit-cli", record.run_id)
            stopped.reached()
            exit_code, _out = stopped.terminate()
        first = SessionStore(store_dir=store_dir).load(record.run_id).submission
        assert first["remote_write_intent"] is False
        assert _branches(offline_remotes) == {r: ["main"] for r in offline_remotes.bare}

        assert _submit(record.run_id) == 0

        final = SessionStore(store_dir=store_dir).load(record.run_id).submission
        assert exit_code == (-signal.SIGKILL if stop == "SIGKILL" else 128 + signal.SIGTERM)
        assert final["status"] == "complete"
        assert final["manifest_sha256"] == first["manifest_sha256"]
        assert final["previous_attempts"] == [first]
        branch = f"apprentice/{record.run_id}"
        assert _branches(offline_remotes) == {r: [branch, "main"] for r in offline_remotes.bare}
        assert len(offline_remotes.gh_calls()) == 2
        ledger = _ledger_rows(store_dir, first["attempt_id"])
        assert ledger["attempt"] == ("ended-before-any-remote-write", 0)
        assert ledger["slots"] == ["released", "released"]
        assert ledger["cycle"] == ("owner-lost" if stop == "SIGKILL" else "cancelled")
        assert ledger["counted_slots"] == 2

    def test_retry_after_a_kill_once_writing_began_is_refused_and_keeps_its_slots(
        self, store_dir: Path, offline_remotes: OfflineRemotes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record = _approved_run(store_dir)
        killed = _Contender(store_dir, "pull-request", "submit", record.run_id)
        killed.reached()
        killed.kill()
        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        refs = _branches(offline_remotes)
        capsys.readouterr()

        assert _submit(record.run_id) == 1

        assert _last_json(capsys)["submission"] == stored
        assert SessionStore(store_dir=store_dir).load(record.run_id).submission == stored
        assert _branches(offline_remotes) == refs
        assert offline_remotes.gh_calls() == []
        ledger = _ledger_rows(store_dir, stored["attempt_id"])
        assert ledger["attempt"] == ("failed-after-write", 1)
        assert ledger["slots"] == ["used", "used"]

    @pytest.mark.parametrize(
        "tamper", ["another-runs-attempt", "other-manifest", "unknown-attempt"]
    )
    def test_a_record_naming_proof_it_does_not_own_is_never_published_again(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        tamper: str,
    ) -> None:
        # Run A ended before writing (its attempt is a genuine NO_WRITE proof).
        # Run B began writing — or, for "other-manifest", ended before writing
        # too — and its record is edited to name proof it does not own.
        run_a = _approved_run(store_dir)
        run_b = _approved_run(store_dir)
        point_b = "effects" if tamper == "other-manifest" else "pull-request"
        for run, point in ((run_a, "effects"), (run_b, point_b)):
            killed = _Contender(store_dir, point, "submit", run.run_id)
            killed.reached()
            killed.kill()
        store = SessionStore(store_dir=store_dir)
        proof = store.load(run_a.run_id).submission
        assert _submit(run_a.run_id) == 0
        record_b = store.load(run_b.run_id)
        edited = {**record_b.submission, "remote_write_intent": False}
        if tamper == "another-runs-attempt":
            edited["attempt_id"] = proof["attempt_id"]
        elif tamper == "other-manifest":
            edited["manifest_sha256"] = "0" * 64
        else:
            edited["attempt_id"] = "0" * 32
        record_b.submission = edited
        store.save(record_b)
        refs = _branches(offline_remotes)
        calls = offline_remotes.gh_calls()
        capsys.readouterr()

        assert _submit(run_b.run_id) == 1

        assert store.load(run_b.run_id).submission == edited
        assert store.load(run_b.run_id).approval == record_b.approval
        assert _branches(offline_remotes) == refs
        assert offline_remotes.gh_calls() == calls


def _ledger_rows(store_dir: Path, attempt_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
    try:
        attempt = conn.execute(
            "SELECT state, remote_write_intent, cycle_id FROM publication_attempts "
            "WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        slots = [
            r[0]
            for r in conn.execute(
                "SELECT state FROM pr_slots WHERE attempt_id = ? ORDER BY repository", (attempt_id,)
            )
        ]
        (outcome,) = conn.execute(
            "SELECT outcome FROM cycles WHERE cycle_id = ?", (attempt[2],)
        ).fetchone()
        (counted,) = conn.execute(
            "SELECT COUNT(*) FROM pr_slots WHERE state IN ('reserved', 'used', 'legacy')"
        ).fetchone()
    finally:
        conn.close()
    return {"attempt": attempt[:2], "slots": slots, "cycle": outcome, "counted_slots": counted}


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


_REPOSITORIES = ("no-magic-ai/no-magic", "no-magic-ai/no-magic-viz")


def _day_quota_config(limit: int) -> Path:
    from tests.conftest import submit_test_config

    path = submit_test_config()
    text = path.read_text(encoding="utf-8").replace(
        "max_prs_per_day = 20", f"max_prs_per_day = {limit}"
    )
    quota = path.with_name(f"day-quota-{limit}.toml")
    quota.write_text(text, encoding="utf-8")
    return quota


def _older_binary_publishes(store_dir: Path, run_id: str, attempt_id: str | None = None) -> None:
    """An earlier apprentice (no ledger) publishes the run, starting now.

    Its submission carries no attempt ID, or `attempt_id` copied into the record.
    """
    from datetime import UTC, datetime

    store = SessionStore(store_dir=store_dir)
    record = store.load(run_id)
    record.submission = {
        "status": "complete",
        "started_at": datetime.now(tz=UTC).isoformat(),
        "manifest_sha256": record.manifest_sha256,
        "branch": f"apprentice/{run_id}",
        "repositories": [
            {
                "repository": repository,
                "pushed": True,
                "pr_url": f"https://github.com/{repository}/pull/older",
            }
            for repository in _REPOSITORIES
        ],
    }
    if attempt_id is not None:
        record.submission["attempt_id"] = attempt_id
    store.save(record)


def _slots(store_dir: Path) -> dict[str, int]:
    conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
    try:
        return dict(conn.execute("SELECT state, COUNT(*) FROM pr_slots GROUP BY state").fetchall())
    finally:
        conn.close()


def _legacy_slots(store_dir: Path) -> dict[str, int]:
    conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
    try:
        rows = conn.execute(
            "SELECT run_id, COUNT(*) FROM pr_slots WHERE state = 'legacy' GROUP BY run_id"
        ).fetchall()
    finally:
        conn.close()
    return dict(rows)


def _submit_with_day_quota(run_id: str, limit: int) -> int:
    """Submit `run_id` in a fresh authority (a restart) with `limit` PRs per day."""
    return run_submit(
        SimpleNamespace(algorithm="selection", run_id=run_id, tier=None), _day_quota_config(limit)
    )


def _refresh(store_dir: Path) -> None:
    authority = Authority.open(store_dir, Footprint(existing=()))
    authority.status()
    authority.close()


class TestForeignPublicationOfAClaimedRun:
    @pytest.mark.parametrize("claim_end", ["killed-at-the-claim", "refused-after-admission"])
    def test_older_binary_publication_of_a_run_this_authority_claimed_holds_its_slots(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        claim_end: str,
    ) -> None:
        from apprentice.controls.policy import ControlPolicy
        from apprentice.core.config import load_config

        claimed = _approved_run(store_dir)
        later = _approved_run(store_dir)
        if claim_end == "killed-at-the-claim":
            killed = _Contender(store_dir, "claim", "submit", claimed.run_id)
            killed.reached()
            killed.kill()
        else:
            authority = Authority.open(store_dir, Footprint(existing=()))
            policy = ControlPolicy.from_config(load_config(_day_quota_config(2)))
            cycle = authority.begin_cycle(
                "submit",
                policy,
                run_id=claimed.run_id,
                publication_claim=(claimed.manifest_sha256, _REPOSITORIES),
            )
            cycle.finish("denied", "the run's claim could not be saved")
            authority.close()
        _older_binary_publishes(store_dir, claimed.run_id)
        refs = _branches(offline_remotes)
        capsys.readouterr()

        code = run_submit(
            SimpleNamespace(algorithm="selection", run_id=later.run_id, tier=None),
            _day_quota_config(2),
        )

        assert code == 1
        assert _last_json(capsys)["control"] == "rate_limits.max_prs_per_day"
        assert _slots(store_dir) == {"released": 2, "legacy": 2}
        assert _branches(offline_remotes) == refs
        assert SessionStore(store_dir=store_dir).load(later.run_id).submission == {}

    def test_older_binary_publication_of_an_unclaimed_run_holds_its_slots(
        self, store_dir: Path, offline_remotes: OfflineRemotes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        unclaimed = _approved_run(store_dir)
        later = _approved_run(store_dir)
        _older_binary_publishes(store_dir, unclaimed.run_id)
        capsys.readouterr()

        code = run_submit(
            SimpleNamespace(algorithm="selection", run_id=later.run_id, tier=None),
            _day_quota_config(2),
        )

        assert code == 1
        assert _last_json(capsys)["control"] == "rate_limits.max_prs_per_day"
        assert _slots(store_dir) == {"legacy": 2}

    def test_own_attempts_count_once_and_a_later_foreign_submission_counts_again(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        published = _approved_run(store_dir)
        assert _submit(published.run_id) == 0
        retried = _approved_run(store_dir)
        killed = _Contender(store_dir, "effects", "submit", retried.run_id)
        killed.reached()
        killed.kill()
        assert _submit(retried.run_id) == 0
        _refresh(store_dir)
        _refresh(store_dir)
        own = _slots(store_dir)

        _older_binary_publishes(store_dir, published.run_id)
        _refresh(store_dir)
        _refresh(store_dir)
        first_foreign = _slots(store_dir)
        _older_binary_publishes(store_dir, published.run_id)
        _refresh(store_dir)

        assert own == {"used": 4, "released": 2}
        assert first_foreign == {"used": 4, "released": 2, "legacy": 2}
        assert _slots(store_dir) == {"used": 4, "released": 2, "legacy": 4}

    def test_older_binary_publications_naming_another_runs_attempt_are_each_counted_once(
        self, store_dir: Path, offline_remotes: OfflineRemotes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        owner = _approved_run(store_dir)
        assert _submit(owner.run_id) == 0
        store = SessionStore(store_dir=store_dir)
        attempt = store.load(owner.run_id).submission["attempt_id"]
        borrowers = [_approved_run(store_dir), _approved_run(store_dir)]
        for borrower in borrowers:
            _older_binary_publishes(store_dir, borrower.run_id)
            record = store.load(borrower.run_id)
            record.submission = {**record.submission, "attempt_id": attempt}
            store.save(record)
        later = _approved_run(store_dir)
        refs = _branches(offline_remotes)
        capsys.readouterr()

        code = run_submit(
            SimpleNamespace(algorithm="selection", run_id=later.run_id, tier=None),
            _day_quota_config(6),
        )
        _refresh(store_dir)

        conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
        legacy = dict(
            conn.execute(
                "SELECT run_id, COUNT(*) FROM pr_slots WHERE state = 'legacy' GROUP BY run_id"
            ).fetchall()
        )
        conn.close()
        assert code == 1
        assert _last_json(capsys)["control"] == "rate_limits.max_prs_per_day"
        assert legacy == {borrower.run_id: 2 for borrower in borrowers}
        assert _slots(store_dir) == {"used": 2, "legacy": 4}
        assert _branches(offline_remotes) == refs
        assert SessionStore(store_dir=store_dir).load(later.run_id).submission == {}

    def test_two_new_runs_sharing_one_foreign_attempt_id_are_both_counted_at_first_admission(
        self, store_dir: Path, offline_remotes: OfflineRemotes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No status or other rescan before the submit: its own admission sees both at once.
        sharing = [_approved_run(store_dir), _approved_run(store_dir)]
        for run in sharing:
            _older_binary_publishes(store_dir, run.run_id, attempt_id="f" * 32)
        later = _approved_run(store_dir)
        refs = _branches(offline_remotes)
        capsys.readouterr()

        code = _submit_with_day_quota(later.run_id, 4)

        assert code == 1
        assert _last_json(capsys)["control"] == "rate_limits.max_prs_per_day"
        assert _legacy_slots(store_dir) == {run.run_id: 2 for run in sharing}
        assert _branches(offline_remotes) == refs
        assert SessionStore(store_dir=store_dir).load(later.run_id).submission == {}

    def test_a_later_run_copying_a_registered_foreign_attempt_id_is_counted_after_restart(
        self, store_dir: Path, offline_remotes: OfflineRemotes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = _approved_run(store_dir)
        _older_binary_publishes(store_dir, first.run_id, attempt_id="e" * 32)
        _refresh(store_dir)
        registered = _legacy_slots(store_dir)
        copier = _approved_run(store_dir)
        _older_binary_publishes(store_dir, copier.run_id, attempt_id="e" * 32)
        later = _approved_run(store_dir)
        refs = _branches(offline_remotes)
        capsys.readouterr()

        code = _submit_with_day_quota(later.run_id, 4)

        assert registered == {first.run_id: 2}
        assert code == 1
        assert _last_json(capsys)["control"] == "rate_limits.max_prs_per_day"
        assert _legacy_slots(store_dir) == {first.run_id: 2, copier.run_id: 2}
        assert _branches(offline_remotes) == refs

    def test_each_new_submission_of_a_run_copying_an_owned_attempt_id_is_counted(
        self, store_dir: Path, offline_remotes: OfflineRemotes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        owner = _approved_run(store_dir)
        assert _submit(owner.run_id) == 0
        owned = SessionStore(store_dir=store_dir).load(owner.run_id).submission["attempt_id"]
        borrower = _approved_run(store_dir)
        _older_binary_publishes(store_dir, borrower.run_id, attempt_id=owned)
        _refresh(store_dir)
        first = _legacy_slots(store_dir)
        # After a restart, the same run is published again with the same copied ID.
        _older_binary_publishes(store_dir, borrower.run_id, attempt_id=owned)
        later = _approved_run(store_dir)
        refs = _branches(offline_remotes)
        capsys.readouterr()

        code = _submit_with_day_quota(later.run_id, 7)

        assert first == {borrower.run_id: 2}
        assert code == 1
        assert _last_json(capsys)["control"] == "rate_limits.max_prs_per_day"
        assert _legacy_slots(store_dir) == {borrower.run_id: 4}
        assert _slots(store_dir) == {"used": 2, "legacy": 4}
        assert _branches(offline_remotes) == refs


class TestSubmitIntoAnUnlistableStore:
    @pytest.mark.parametrize("mode", [0o300, 0o100], ids=["write-and-search-only", "search-only"])
    def test_a_known_foreign_publication_that_cannot_be_listed_is_never_skipped(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        capsys: pytest.CaptureFixture[str],
        mode: int,
    ) -> None:
        foreign = _approved_run(store_dir)
        _older_binary_publishes(store_dir, foreign.run_id)
        later = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        database = store_dir / "controls" / "accounting.sqlite3"
        ledger, refs = database.read_bytes(), _branches(offline_remotes)
        original = store_dir.stat().st_mode & 0o777
        capsys.readouterr()
        store_dir.chmod(mode)
        try:
            code = _submit_with_day_quota(later.run_id, 2)
        finally:
            store_dir.chmod(original)

        out = _last_json(capsys)
        assert code == 1
        assert str(store_dir) in out["error"]
        assert database.read_bytes() == ledger
        assert SessionStore(store_dir=store_dir).load(later.run_id).submission == {}
        assert _branches(offline_remotes) == refs
        assert offline_remotes.gh_calls() == []


class TestOperatorStopAfterTheWriteIntent:
    def test_stop_after_both_pushes_records_the_known_effects_and_is_never_retried(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        stopped = _Contender(store_dir, "pull-request", "submit-cli", record.run_id)
        stopped.reached()

        code, out = stopped.terminate()

        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        branch = f"apprentice/{record.run_id}"
        ledger = _ledger_rows(store_dir, stored["attempt_id"])
        reported = json.loads(out[out.rfind("\n{") + 1 :])
        assert code == 128 + signal.SIGTERM
        assert (reported["outcome"], reported["run_id"]) == ("cancelled", record.run_id)
        assert str(store_dir) not in reported["error"]  # the record was updated
        assert reported["cycle_id"] == _attempt_cycle(store_dir, stored["attempt_id"])
        assert (stored["status"], stored["remote_write_intent"]) == ("partial", True)
        assert [r["pushed"] for r in stored["repositories"]] == [True, True]
        assert ledger["attempt"] == ("failed-after-write", 1)
        assert ledger["slots"] == ["used", "used"]
        assert ledger["cycle"] == "cancelled"
        assert _submit(record.run_id) == 1
        assert _branches(offline_remotes) == {r: [branch, "main"] for r in offline_remotes.bare}
        assert offline_remotes.gh_calls() == []

    @pytest.mark.parametrize("point", ["attempt-end", "cycle-end"])
    def test_stop_while_the_end_is_committed_keeps_the_completed_publication(
        self, store_dir: Path, offline_remotes: OfflineRemotes, point: str
    ) -> None:
        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        stopped = _Contender(store_dir, point, "submit-cli", record.run_id)
        stopped.reached()

        code, out = stopped.terminate()

        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        ledger = _ledger_rows(store_dir, stored["attempt_id"])
        reported = json.loads(out[out.rfind("\n{") + 1 :])
        assert code == 128 + signal.SIGTERM
        assert (reported["outcome"], reported["run_id"]) == ("completed", record.run_id)
        assert reported["cycle_id"] == _attempt_cycle(store_dir, stored["attempt_id"])
        assert stored["status"] == "complete"
        assert ledger["attempt"] == ("complete", 1)
        assert ledger["slots"] == ["used", "used"]
        assert ledger["cycle"] == "completed"
        assert len(offline_remotes.gh_calls()) == 2
        assert _submit(record.run_id) == 1
        assert len(offline_remotes.gh_calls()) == 2

    def test_stop_while_the_claim_is_saved_ends_before_any_write(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        stopped = _Contender(store_dir, "claim", "submit-cli", record.run_id)
        stopped.reached()

        code, out = stopped.terminate()

        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        ledger = _ledger_rows(store_dir, stored["attempt_id"])
        reported = json.loads(out[out.rfind("\n{") + 1 :])
        assert code == 128 + signal.SIGTERM
        assert (reported["outcome"], reported["run_id"]) == ("cancelled", record.run_id)
        assert str(store_dir) not in reported["error"]  # the claim and the outcome were saved
        assert stored["remote_write_intent"] is False
        assert ledger["attempt"] == ("ended-before-any-remote-write", 0)
        assert ledger["slots"] == ["released", "released"]
        assert ledger["cycle"] == "cancelled"
        assert _branches(offline_remotes) == {r: ["main"] for r in offline_remotes.bare}
        assert offline_remotes.gh_calls() == []

    @pytest.mark.parametrize("mode", [0o500, 0o555])
    def test_stop_during_the_publication_names_the_record_it_could_not_update(
        self, store_dir: Path, offline_remotes: OfflineRemotes, mode: int
    ) -> None:
        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        original = store_dir.stat().st_mode & 0o777
        path = store_dir / f"{record.run_id}.json"
        stopped = _Contender(store_dir, "pull-request", "submit-cli", record.run_id)
        stopped.reached()
        store_dir.chmod(mode)  # the outcome save after the interruption is refused by the OS
        try:
            code, out = stopped.terminate()
        finally:
            store_dir.chmod(original)

        reported = _json_objects(out)
        final, unsaved = reported[-1], next(o for o in reported if "reserved" in o)
        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        ledger = _ledger_rows(store_dir, stored["attempt_id"])
        pushed = {r: [f"apprentice/{record.run_id}", "main"] for r in offline_remotes.bare}
        assert code == 128 + signal.SIGTERM
        assert (final["outcome"], final["run_id"]) == ("cancelled", record.run_id)
        assert str(path) in final["error"]
        assert final["error"].count(f"[Errno {errno.EACCES}]") == 1
        assert stored == unsaved["stored"] == unsaved["reserved"]
        assert (stored["status"], stored["remote_write_intent"]) == ("pending", False)
        assert [r["pushed"] for r in unsaved["outcome"]["repositories"]] == [True, True]
        assert ledger["attempt"] == ("failed-after-write", 1)
        assert ledger["slots"] == ["used", "used"]
        assert ledger["cycle"] == "cancelled"
        assert _submit(record.run_id) == 1
        assert _branches(offline_remotes) == pushed
        assert offline_remotes.gh_calls() == []

    @pytest.mark.parametrize("mode", [0o500, 0o555])
    def test_stop_while_an_unsavable_claim_is_saved_names_it_and_writes_nothing(
        self, store_dir: Path, offline_remotes: OfflineRemotes, mode: int
    ) -> None:
        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        original = store_dir.stat().st_mode & 0o777
        path = store_dir / f"{record.run_id}.json"
        data, refs = path.read_bytes(), _branches(offline_remotes)
        stopped = _Contender(store_dir, "claim", "submit-cli", record.run_id)
        stopped.reached()
        (scratch,) = (store_dir / "scratch").iterdir()  # allocated before the claim save
        store_dir.chmod(mode)
        try:
            code, out = stopped.terminate()
        finally:
            store_dir.chmod(original)

        final = _json_objects(out)[-1]
        conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
        attempts = conn.execute(
            "SELECT state, remote_write_intent FROM publication_attempts"
        ).fetchall()
        slots = sorted(r[0] for r in conn.execute("SELECT state FROM pr_slots"))
        cycles = conn.execute("SELECT outcome FROM cycles WHERE kind = 'submit'").fetchall()
        conn.close()
        assert code == 128 + signal.SIGTERM
        assert (final["outcome"], final["run_id"]) == ("denied", record.run_id)
        assert str(path) in final["error"] and str(scratch) in final["error"]
        assert final["error"].count(f"[Errno {errno.EACCES}]") == 1
        assert attempts == [("ended-before-any-remote-write", 0)]
        assert slots == ["released", "released"]
        assert cycles == [("denied",)]
        assert path.read_bytes() == data
        assert _branches(offline_remotes) == refs
        assert offline_remotes.gh_calls() == []

    def test_stop_while_an_unsavable_outcome_is_recorded_keeps_the_known_effects_visible(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        original = store_dir.stat().st_mode & 0o777
        stopped = _Contender(store_dir, "outcome-save", "submit-cli", record.run_id)
        stopped.reached()
        try:
            code, out = stopped.terminate()
        finally:
            store_dir.chmod(original)

        reported = _json_objects(out)
        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        ledger = _ledger_rows(store_dir, stored["attempt_id"])
        unsaved = next(o for o in reported if "reserved" in o)
        assert code == 128 + signal.SIGTERM
        assert (reported[-1]["outcome"], reported[-1]["run_id"]) == ("completed", record.run_id)
        assert str(store_dir) in reported[-1]["error"]
        assert all(r["pushed"] and r["pr_url"] for r in unsaved["outcome"]["repositories"])
        assert stored == unsaved["stored"] == unsaved["reserved"]
        assert ledger["attempt"] == ("complete", 1)
        assert ledger["slots"] == ["used", "used"]
        assert ledger["cycle"] == "completed"
        assert _submit(record.run_id) == 1
        assert len(offline_remotes.gh_calls()) == 2


class TestClaimInAStoreThatCannotBeWritten:
    def test_a_claim_that_cannot_be_saved_ends_before_any_write_and_releases_its_slots(
        self, store_dir: Path, offline_remotes: OfflineRemotes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        path = store_dir / f"{record.run_id}.json"
        data, refs = path.read_bytes(), _branches(offline_remotes)
        capsys.readouterr()
        original = store_dir.stat().st_mode & 0o777
        store_dir.chmod(0o555)
        try:
            code = _submit(record.run_id)
        finally:
            store_dir.chmod(original)

        out = _last_json(capsys)
        conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
        attempts = conn.execute(
            "SELECT state, remote_write_intent FROM publication_attempts"
        ).fetchall()
        slots = sorted(r[0] for r in conn.execute("SELECT state FROM pr_slots"))
        cycles = conn.execute("SELECT outcome FROM cycles WHERE kind = 'submit'").fetchall()
        conn.close()
        assert code == 1
        assert out["run_id"] == record.run_id and str(store_dir) in out["error"]
        assert attempts == [("ended-before-any-remote-write", 0)]
        assert slots == ["released", "released"]
        assert cycles == [("denied",)]
        assert path.read_bytes() == data
        assert _branches(offline_remotes) == refs
        assert offline_remotes.gh_calls() == []

    def test_a_later_stopped_command_does_not_report_this_refusal(
        self,
        store_dir: Path,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from apprentice import cli
        from tests.conftest import submit_test_config

        record = _approved_run(store_dir)
        Authority.open(store_dir, Footprint(existing=())).close()
        submit = ["--config", str(submit_test_config()), "submit", "selection"]
        submit += ["--run-id", record.run_id]
        path = store_dir / f"{record.run_id}.json"
        original = store_dir.stat().st_mode & 0o777
        allocate = SessionStore.allocate_work_root
        scratch: list[Path] = []

        def allocate_then_refuse_the_claim(self: SessionStore) -> Path:
            scratch.append(allocate(self))
            store_dir.chmod(0o555)  # the claim's save, next, is refused by the OS
            return scratch[-1]

        capsys.readouterr()
        with monkeypatch.context() as first:
            first.setattr(SessionStore, "allocate_work_root", allocate_then_refuse_the_claim)
            try:
                refused = main(submit)
            finally:
                store_dir.chmod(original)
        refusal = _json_objects(capsys.readouterr().out)[-1]
        finish = cli._finish_submission

        def stop_then_finish(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            # Deferred: the stop is raised fresh when the submit's records are committed.
            os.kill(os.getpid(), signal.SIGTERM)
            return finish(*args, **kwargs)

        monkeypatch.setattr(cli, "_finish_submission", stop_then_finish)
        code = main(submit)

        final = _json_objects(capsys.readouterr().out)[-1]
        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
        attempts = conn.execute(
            "SELECT state, remote_write_intent FROM publication_attempts ORDER BY claimed_at"
        ).fetchall()
        cycles = conn.execute(
            "SELECT outcome FROM cycles WHERE kind = 'submit' ORDER BY admitted_at"
        ).fetchall()
        conn.close()
        assert refused == 1
        assert str(path) in refusal["error"]
        assert f"[Errno {errno.EACCES}]" in refusal["error"]
        assert code == 128 + signal.SIGTERM
        assert (final["outcome"], final["run_id"]) == ("completed", record.run_id)
        assert str(path) not in final["error"] and str(scratch[0]) not in final["error"]
        assert stored["status"] == "complete"
        assert attempts == [("ended-before-any-remote-write", 0), ("complete", 1)]
        assert cycles == [("denied",), ("completed",)]
        assert len(offline_remotes.gh_calls()) == 2


def _json_objects(text: str) -> list[dict[str, Any]]:
    decoder, objects, index = json.JSONDecoder(), [], 0
    text = text.strip()
    while index < len(text):
        while text[index].isspace():
            index += 1
        value, index = decoder.raw_decode(text, index)
        objects.append(value)
    return objects


def _attempt_cycle(store_dir: Path, attempt_id: str) -> str:
    conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
    try:
        (cycle_id,) = conn.execute(
            "SELECT cycle_id FROM publication_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
    finally:
        conn.close()
    return str(cycle_id)


def _circuit_config() -> Path:
    """The submit test config with a circuit that opens on one failure and probes at once."""
    from tests.conftest import submit_test_config

    path = submit_test_config()
    text = path.read_text(encoding="utf-8")
    for old, new in (
        ("failure_threshold = 3", "failure_threshold = 1"),
        ("half_open_probe_after_minutes = 60", "half_open_probe_after_minutes = 0"),
    ):
        assert old in text
        text = text.replace(old, new)
    circuit = path.with_name("circuit.toml")
    circuit.write_text(text, encoding="utf-8")
    return circuit


class TestHalfOpenProbeSubmit:
    def test_submit_admitted_as_the_probe_writes_and_closes_the_circuit(
        self, store_dir: Path, offline_remotes: OfflineRemotes
    ) -> None:
        from apprentice.controls.policy import ControlPolicy
        from apprentice.core.config import load_config

        record = _approved_run(store_dir)
        config = _circuit_config()
        authority = Authority.open(store_dir, Footprint(existing=()))
        failed = authority.begin_cycle("build", ControlPolicy.from_config(load_config(config)))
        failed.finish("failed")
        opened = authority.status()["circuit"]["state"]
        authority.close()
        args = SimpleNamespace(algorithm="selection", run_id=record.run_id, tier=None)

        code = run_submit(args, config)

        stored = SessionStore(store_dir=store_dir).load(record.run_id).submission
        ledger = _ledger_rows(store_dir, stored["attempt_id"])
        authority = Authority.open(store_dir, Footprint(existing=()))
        closed = authority.status()["circuit"]["state"]
        authority.close()
        assert (opened, closed) == ("open", "closed")
        assert code == 0
        assert ledger["attempt"] == ("complete", 1)
        assert len(offline_remotes.gh_calls()) == 2
