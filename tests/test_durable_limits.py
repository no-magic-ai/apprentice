"""Durable side-effect limits: items, cooldown, the circuit, PR windows and publication.

Admission is exercised through the installation authority (and the real CLI
submit path with offline bare repositories and a recording `gh`). Time is the
authority's UTC clock, controlled here; nothing is published remotely.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import apprentice.controls.authority as authority_module
from apprentice.agents.packaging import measure_commit
from apprentice.cli import _cmd_approve
from apprentice.controls.authority import Authority
from apprentice.controls.errors import ControlDeniedError
from apprentice.controls.footprint import Footprint
from apprentice.controls.policy import ControlPolicy
from apprentice.core.config import load_config
from apprentice.core.session_store import SessionStore, default_store_dir
from tests.conftest import run_submit
from tests.responses_fixture import ResponsesFixture, ResponsesServer, local_profile, write_config

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.conftest import OfflineRemotes

_EMPTY = Footprint(existing=())


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(tz=UTC) + timedelta(minutes=1)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    controlled = Clock()
    monkeypatch.setattr(authority_module, "utc_now", controlled)
    return controlled


def _policy(tmp_path: Path, **limits: Any) -> ControlPolicy:
    path = write_config(
        tmp_path / f"limits-{len(list(tmp_path.glob('limits-*')))}.toml",
        profile=local_profile(tmp_path / "profile.json"),
        base_url="http://127.0.0.1:9/v1",
        **limits,
    )
    return ControlPolicy.from_config(load_config(path))


@pytest.fixture
def installation(tmp_path: Path) -> Iterator[Authority]:
    root = tmp_path / "installation"
    root.mkdir()
    authority = Authority.open(root, _EMPTY)
    yield authority
    authority.close()


def _denied(authority: Authority, policy: ControlPolicy, kind: str = "build") -> str:
    with pytest.raises(ControlDeniedError) as denied:
        authority.begin_cycle(kind, policy)
    return denied.value.control


class TestConcurrentItems:
    def test_a_live_cycle_in_another_process_blocks_the_next_item(self, tmp_path: Path) -> None:
        fixture = ResponsesFixture(outputs={"discovery": "[]"}, gate=threading.Event())
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
            )
            store = tmp_path / "store"
            store.mkdir()
            Authority.open(store, _EMPTY).close()
            worker = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).parent / "control_worker.py"),
                    str(store),
                    str(config),
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert worker.stdout is not None
                assert worker.stdout.readline().strip() == "ADMITTED"
                assert fixture.arrived.wait(timeout=30)
                authority = Authority.open(store, _EMPTY)
                policy = ControlPolicy.from_config(load_config(config))

                blocked = _denied(authority, policy, "suggest")
                fixture.gate.set()  # type: ignore[union-attr]
                assert worker.stdout.readline().strip() == "FINISHED"
                worker.wait(timeout=30)
                with authority.begin_cycle("suggest", policy):
                    pass
            finally:
                if worker.poll() is None:
                    os.kill(worker.pid, signal.SIGKILL)

        assert blocked == "rate_limits.max_concurrent_items"
        assert worker.returncode == 0


class TestCooldown:
    def test_next_cycle_waits_exactly_the_configured_hours_across_restart(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        policy = _policy(tmp_path, cooldown_hours=4)
        with installation.begin_cycle("suggest", policy) as cycle:
            cycle.finish("completed")
        clock.advance(timedelta(hours=4) - timedelta(microseconds=1))
        restarted = Authority.open(installation.store_dir, _EMPTY)

        early = _denied(restarted, policy)
        clock.advance(timedelta(microseconds=1))
        with restarted.begin_cycle("build", policy):
            pass

        assert early == "rate_limits.cooldown_hours"

    def test_denied_admission_does_not_move_the_anchor(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        policy = _policy(tmp_path, cooldown_hours=1)
        with installation.begin_cycle("suggest", policy):
            pass
        clock.advance(timedelta(minutes=59))
        _denied(installation, policy)
        clock.advance(timedelta(minutes=1))

        with installation.begin_cycle("build", policy):
            pass


class TestCircuit:
    def _fail(self, authority: Authority, policy: ControlPolicy, outcome: str = "failed") -> None:
        cycle = authority.begin_cycle("build", policy)
        cycle.finish(outcome)

    def test_threshold_counts_only_qualifying_outcomes(
        self, tmp_path: Path, installation: Authority
    ) -> None:
        policy = _policy(tmp_path, failure_threshold=2)
        self._fail(installation, policy)
        self._fail(installation, policy, "denied")
        self._fail(installation, policy, "cancelled")
        with installation.begin_cycle("build", policy):
            pass
        self._fail(installation, policy)
        self._fail(installation, policy)

        assert _denied(installation, policy) == "circuit_breaker.failure_threshold"

    def test_one_probe_after_the_deadline_then_close_on_success(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        policy = _policy(
            tmp_path, failure_threshold=1, half_open_probe_after_minutes=60, max_concurrent_items=2
        )
        self._fail(installation, policy)
        clock.advance(timedelta(minutes=60) - timedelta(microseconds=1))
        before = _denied(installation, policy)
        clock.advance(timedelta(microseconds=1))

        probe = installation.begin_cycle("build", policy)
        second = _denied(installation, policy)
        probe.finish("completed")
        with installation.begin_cycle("build", policy):
            pass

        assert before == "circuit_breaker.failure_threshold"
        assert second == "circuit_breaker.half_open_probe_after_minutes"

    def test_neutral_probe_releases_without_closing(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        policy = _policy(tmp_path, failure_threshold=1, half_open_probe_after_minutes=1)
        self._fail(installation, policy)
        clock.advance(timedelta(minutes=1))
        installation.begin_cycle("build", policy).finish("denied")

        assert installation.status()["circuit"]["state"] == "open"
        probe = installation.begin_cycle("build", policy)
        probe.finish("completed")
        assert installation.status()["circuit"]["state"] == "closed"

    def test_consecutive_opens_latch_until_a_manual_reset_that_keeps_every_row(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        policy = _policy(
            tmp_path,
            failure_threshold=1,
            half_open_probe_after_minutes=1,
            max_open_cycles_before_manual_reset=2,
        )
        self._fail(installation, policy)
        clock.advance(timedelta(minutes=1))
        self._fail(installation, policy)
        clock.advance(timedelta(days=1))
        latched = _denied(installation, policy)
        rows_before = installation.status()
        live = installation.begin_cycle  # a live cycle refuses the reset

        reset = installation.reset_circuit("operator@example.invalid")
        with live("build", policy), pytest.raises(ControlDeniedError):
            installation.reset_circuit("operator@example.invalid")

        assert latched == "circuit_breaker.max_open_cycles_before_manual_reset"
        assert reset["previous_state"] == "latched"
        assert installation.status()["month_entries"] == rows_before["month_entries"]
        assert installation.status()["pr_windows"] == rows_before["pr_windows"]

    def test_owner_loss_is_a_qualifying_failure(
        self, tmp_path: Path, installation: Authority
    ) -> None:
        policy = _policy(tmp_path, failure_threshold=1)
        orphan = installation.begin_cycle("build", policy)
        os.close(orphan._lease_fd)  # type: ignore[arg-type]
        orphan._lease_fd = None  # the owning process is gone; its lease is released

        assert _denied(installation, policy) == "circuit_breaker.failure_threshold"


class TestPrWindows:
    def test_day_and_week_windows_roll_from_each_claim(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        policy = _policy(tmp_path, max_prs_per_day=2, max_prs_per_week=3, max_concurrent_items=5)
        claim = ("m" * 64, ("no-magic-ai/no-magic", "no-magic-ai/no-magic-viz"))
        first = installation.begin_cycle(
            "submit", policy, run_id="selection-a", publication_claim=claim
        )
        installation.begin_writes(first)
        first.finish("completed")
        with pytest.raises(ControlDeniedError) as day:
            installation.begin_cycle(
                "submit", policy, run_id="selection-b", publication_claim=(claim[0], claim[1][:1])
            )
        clock.advance(timedelta(hours=24) - timedelta(microseconds=1))
        with pytest.raises(ControlDeniedError):
            installation.begin_cycle(
                "submit", policy, run_id="selection-b", publication_claim=(claim[0], claim[1][:1])
            )
        clock.advance(timedelta(microseconds=1))
        second = installation.begin_cycle(
            "submit", policy, run_id="selection-b", publication_claim=(claim[0], claim[1][:1])
        )
        installation.begin_writes(second)
        second.finish("completed")
        with pytest.raises(ControlDeniedError) as week:
            installation.begin_cycle(
                "submit", policy, run_id="selection-c", publication_claim=(claim[0], claim[1][:1])
            )

        assert day.value.control == "rate_limits.max_prs_per_day"
        assert week.value.control == "rate_limits.max_prs_per_week"

    def test_slots_of_an_attempt_that_never_wrote_are_released(
        self, tmp_path: Path, installation: Authority
    ) -> None:
        policy = _policy(tmp_path, max_prs_per_day=1)
        claim = ("m" * 64, ("no-magic-ai/no-magic",))
        cycle = installation.begin_cycle(
            "submit", policy, run_id="selection-a", publication_claim=claim
        )
        assert cycle.publication_attempt is not None
        installation.finish_publication(
            cycle.publication_attempt, "ended-before-any-remote-write", "x"
        )
        cycle.finish("denied")

        with installation.begin_cycle(
            "submit", policy, run_id="selection-a", publication_claim=claim
        ):
            pass


def _write_file(repo: Path, name: str, data: bytes) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class TestChangeMeasurement:
    def test_text_lines_renames_and_binary_files_are_classified(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        _write_file(repo, "old.py", b"".join(b"line %d\n" % i for i in range(20)))
        _write_file(repo, "edit.txt", b"a\nb\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "base")
        _git(repo, "mv", "old.py", "new.py")
        _write_file(repo, "edit.txt", b"a\nc\nunterminated")
        _write_file(repo, "media/preview.gif", b"GIF89a\x00\x01\x02" * 100_000)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "change")

        size = measure_commit(repo)

        assert size.files == 4  # renamed old.py + new.py, edit.txt, the binary
        assert size.text_lines == 3  # edit.txt: +c +unterminated -b; the pure rename has none
        assert size.binary == (("media/preview.gif", 900_000),)


def _approved(store_dir: Path, implementation: str) -> str:
    store = SessionStore(store_dir=store_dir)
    record = store.create_run("selection", 2)
    store.complete_run(
        record,
        {
            "algorithm_name": "selection",
            "generated_code": implementation,
            "manim_scene_code": "print('scene')\n",
            "anki_deck_content": "front,back\n",
        },
        {},
        1.0,
    )
    assert _cmd_approve(SimpleNamespace(run_id=record.run_id, approver="tester")) == 0
    return record.run_id


def _limited_config(**limits: str) -> Path:
    path = Path.home() / "submit-limited.toml"
    text = (Path(__file__).parent.parent / "config" / "apprentice.toml").read_text()
    text = text.replace("cooldown_hours = 4", "cooldown_hours = 0")
    for key, value in limits.items():
        start = text.index(f"{key} = ")
        end = text.index("\n", start)
        text = text[:start] + f"{key} = {value}" + text[end:]
    path.write_text(text)
    return path


def _submission(run_id: str) -> dict[str, Any]:
    return SessionStore(store_dir=default_store_dir()).load(run_id).submission


class TestPublicationLimits:
    def test_text_line_limit_equality_publishes_and_one_more_line_is_refused_before_any_push(
        self, offline_remotes: OfflineRemotes
    ) -> None:
        store_dir = default_store_dir()
        exact = _approved(store_dir, "".join(f"x{i} = {i}\n" for i in range(3)))
        over = _approved(store_dir, "".join(f"x{i} = {i}\n" for i in range(4)))
        args = SimpleNamespace(algorithm="selection", tier=None)
        config = _limited_config(max_lines_per_pr="3", max_prs_per_day="20", max_prs_per_week="20")

        refused = run_submit(SimpleNamespace(**vars(args), run_id=over), config)
        published = run_submit(SimpleNamespace(**vars(args), run_id=exact), config)

        assert (refused, published) == (1, 0)
        denied = _submission(over)
        assert denied["status"] == "denied" and denied["remote_write_intent"] is False
        assert f"apprentice/{over}" not in offline_remotes.branches("no-magic-ai/no-magic")
        assert f"apprentice/{exact}" in offline_remotes.branches("no-magic-ai/no-magic")

    def test_attempt_denied_before_writing_can_be_retried_with_the_same_manifest(
        self, offline_remotes: OfflineRemotes
    ) -> None:
        run_id = _approved(default_store_dir(), "x = 1\n")
        args = SimpleNamespace(algorithm="selection", tier=None, run_id=run_id)

        first = run_submit(
            args, _limited_config(max_files_per_pr="0", max_prs_per_day="20", max_prs_per_week="20")
        )
        second = run_submit(
            args, _limited_config(max_files_per_pr="1", max_prs_per_day="20", max_prs_per_week="20")
        )

        stored = _submission(run_id)
        assert (first, second) == (1, 0)
        assert stored["status"] == "complete" and stored["remote_write_intent"] is True
        assert stored["previous_attempts"][0]["status"] == "denied"
        assert stored["previous_attempts"][0]["remote_write_intent"] is False

    def test_attempt_that_began_writing_is_never_retried(
        self, offline_remotes: OfflineRemotes
    ) -> None:
        run_id = _approved(default_store_dir(), "x = 1\n")
        offline_remotes.reject_pushes("no-magic-ai/no-magic")
        args = SimpleNamespace(algorithm="selection", tier=None, run_id=run_id)

        first = run_submit(args)
        second = run_submit(args)

        stored = _submission(run_id)
        assert (first, second) == (1, 1)
        assert stored["remote_write_intent"] is True
        assert "previous_attempts" not in stored
        conn = sqlite3.connect(default_store_dir() / "controls" / "accounting.sqlite3")
        slots = conn.execute("SELECT state FROM pr_slots").fetchall()
        conn.close()
        assert slots == [("used",), ("used",)]

    def test_default_daily_quota_refuses_a_third_pr_before_claiming(
        self, offline_remotes: OfflineRemotes
    ) -> None:
        first = _approved(default_store_dir(), "x = 1\n")
        second = _approved(default_store_dir(), "y = 1\n")
        config = _limited_config()  # shipped 2/day and 5/week, cooldown 0

        published = run_submit(
            SimpleNamespace(algorithm="selection", tier=None, run_id=first), config
        )
        refused = run_submit(
            SimpleNamespace(algorithm="selection", tier=None, run_id=second), config
        )

        assert (published, refused) == (0, 1)
        assert _submission(second) == {}


class TestRollbackAndLegacyPublication:
    def test_prepare_rollback_suspends_until_readoption_holds_the_interval(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        policy = _policy(tmp_path)
        installation.prepare_rollback("operator@example.invalid")
        suspended = _denied(installation, policy)
        first_month = clock.now.strftime("%Y-%m")
        clock.advance(timedelta(days=40))

        restored = installation.adopt_legacy("operator@example.invalid")

        status = installation.status()
        assert suspended == "controls.continuity"
        assert restored["continuity_restored"] is True
        months = [m["month"] for m in status["unknown_months"]]
        assert months[0] == first_month and months[-1] == clock.now.strftime("%Y-%m")
        assert len(months) == 2
        assert status["pr_windows"]["day_filled"] and status["pr_windows"]["week_filled"]

    def test_unreferenced_unsettled_submission_holds_every_window(
        self, tmp_path: Path, installation: Authority
    ) -> None:
        policy = _policy(tmp_path, max_prs_per_day=10)
        (installation.store_dir / "selection-20261001T000000Z.json").write_text(
            json.dumps(
                {
                    "run_id": "selection-20261001T000000Z",
                    "algorithm_name": "selection",
                    "tier": 2,
                    "status": "completed",
                    "started_at": "2026-10-01T00:00:00+00:00",
                    "submission": {
                        "status": "failed",
                        "started_at": "2026-10-01T01:00:00+00:00",
                        "repositories": [
                            {"repository": "no-magic-ai/no-magic", "pushed": None, "pr_url": ""}
                        ],
                    },
                }
            )
        )

        with pytest.raises(ControlDeniedError) as denied:
            installation.begin_cycle(
                "submit",
                policy,
                run_id="selection-x",
                publication_claim=("m" * 64, ("no-magic-ai/no-magic",)),
            )

        assert denied.value.control == "rate_limits.max_prs_per_day"


def _ledger() -> sqlite3.Connection:
    return sqlite3.connect(default_store_dir() / "controls" / "accounting.sqlite3")


def _after_first_push(monkeypatch: pytest.MonkeyPatch, action: Any) -> None:
    """Run `action` once the first `git push` of the submission has completed."""
    import apprentice.agents.packaging as packaging

    real_run = packaging._run
    done: list[int] = []

    def run(args: list[str], *rest: Any, **kwargs: Any) -> bytes:
        out = real_run(args, *rest, **kwargs)
        if args[:2] == ["git", "push"] and not done:
            done.append(1)
            action()
        return out

    monkeypatch.setattr(packaging, "_run", run)


class TestBetweenRemoteSteps:
    def test_own_cycle_ended_after_the_first_push_stops_the_next_push_and_prs(
        self, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _approved(default_store_dir(), "x = 1\n")

        def cancel_own_cycle() -> None:
            conn = _ledger()
            conn.execute(
                "UPDATE cycles SET state = 'terminal', outcome = 'cancelled' "
                "WHERE kind = 'submit' AND state = 'active'"
            )
            conn.commit()
            conn.close()

        _after_first_push(monkeypatch, cancel_own_cycle)

        code = run_submit(SimpleNamespace(algorithm="selection", tier=None, run_id=run_id))

        stored = _submission(run_id)
        assert code == 1
        assert offline_remotes.gh_calls() == []
        assert f"apprentice/{run_id}" not in offline_remotes.branches("no-magic-ai/no-magic-viz")
        assert stored["status"] == "partial" and stored["remote_write_intent"] is True
        assert [r["pushed"] for r in stored["repositories"]] == [True, False]

    def test_unrelated_circuit_opening_after_the_first_push_does_not_abort_the_forest(
        self, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _approved(default_store_dir(), "x = 1\n")

        def open_circuit() -> None:
            conn = _ledger()
            until = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
            conn.execute("UPDATE meta SET value = 'open' WHERE key = 'circuit_state'")
            conn.execute("UPDATE meta SET value = ? WHERE key = 'circuit_open_until'", (until,))
            conn.commit()
            conn.close()

        _after_first_push(monkeypatch, open_circuit)

        code = run_submit(SimpleNamespace(algorithm="selection", tier=None, run_id=run_id))

        assert code == 0
        assert _submission(run_id)["status"] == "complete"
        assert len(offline_remotes.gh_calls()) == 2

    def test_interruption_after_writing_began_keeps_known_and_unknown_effects(
        self, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _approved(default_store_dir(), "x = 1\n")
        import apprentice.agents.packaging as packaging

        real_run = packaging._run

        def run(args: list[str], *rest: Any, **kwargs: Any) -> bytes:
            if args[:2] == ["git", "push"] and "no-magic-viz" in str(rest[0]):
                real_run(args, *rest, **kwargs)
                raise KeyboardInterrupt
            return real_run(args, *rest, **kwargs)

        monkeypatch.setattr(packaging, "_run", run)

        with pytest.raises(KeyboardInterrupt):
            run_submit(SimpleNamespace(algorithm="selection", tier=None, run_id=run_id))
        again = run_submit(SimpleNamespace(algorithm="selection", tier=None, run_id=run_id))

        stored = _submission(run_id)
        assert stored["status"] == "partial" and stored["effects_unknown"] is True
        assert [r["pushed"] for r in stored["repositories"]] == [True, None]
        assert again == 1
        assert offline_remotes.gh_calls() == []


class TestPrewriteSettlement:
    def test_owner_lost_after_claim_releases_the_attempt_and_its_slots_once(
        self, tmp_path: Path, installation: Authority
    ) -> None:
        policy = _policy(tmp_path, max_prs_per_day=2, failure_threshold=5)
        claim = ("m" * 64, ("no-magic-ai/no-magic", "no-magic-ai/no-magic-viz"))
        orphan = installation.begin_cycle(
            "submit", policy, run_id="selection-a", publication_claim=claim
        )
        os.close(orphan._lease_fd)  # type: ignore[arg-type]
        orphan._lease_fd = None  # its process is gone

        with installation.begin_cycle(
            "submit", policy, run_id="selection-a", publication_claim=claim
        ):
            pass

        conn = installation._conn
        states = conn.execute(
            "SELECT state, remote_write_intent FROM publication_attempts ORDER BY claimed_at"
        ).fetchall()
        slots = sorted(r[0] for r in conn.execute("SELECT state FROM pr_slots"))
        assert states == [
            ("ended-before-any-remote-write", 0),
            ("ended-before-any-remote-write", 0),
        ]
        assert slots == ["released"] * 4


class TestSuggestOutput:
    def _suggest(self, tmp_path: Path, output: str) -> tuple[int, dict[str, Any]]:
        from apprentice.cli import main

        fixture = ResponsesFixture(outputs={"discovery": output})
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
                failure_threshold=1,
            )
            code = main(["--config", str(config), "suggest", "--limit", "1"])
            main(["--config", str(config), "status"])
        conn = _ledger()
        circuit = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'circuit_%'"))
        conn.close()
        return code, circuit

    def test_missing_model_output_fails_the_cycle_and_counts_toward_the_circuit(
        self, tmp_path: Path
    ) -> None:
        code, circuit = self._suggest(tmp_path, "")

        assert code == 1
        assert (circuit["circuit_state"], circuit["circuit_open_streak"]) == ("open", "1")

    def test_an_actual_empty_candidate_list_completes(self, tmp_path: Path) -> None:
        code, circuit = self._suggest(tmp_path, "[]")

        assert code == 0
        assert circuit["circuit_state"] == "closed"


def _json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects, index = [], 0
    while index < len(text.rstrip()):
        while text[index].isspace():
            index += 1
        value, index = decoder.raw_decode(text, index)
        objects.append(value)
    return objects


class TestFinalAdmissionAndOwnership:
    def test_circuit_opened_after_preparation_denies_the_first_write_with_zero_effects(
        self, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import apprentice.agents.packaging as packaging

        run_id = _approved(default_store_dir(), "x = 1\n")
        real_prepare = packaging._prepare
        prepared: list[str] = []

        def prepare(repository: str, *args: Any) -> Any:
            submission = real_prepare(repository, *args)
            prepared.append(repository)
            if len(prepared) == 2:
                conn = _ledger()
                until = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
                conn.execute("UPDATE meta SET value = 'open' WHERE key = 'circuit_state'")
                conn.execute("UPDATE meta SET value = ? WHERE key = 'circuit_open_until'", (until,))
                conn.commit()
                conn.close()
            return submission

        monkeypatch.setattr(packaging, "_prepare", prepare)

        code = run_submit(SimpleNamespace(algorithm="selection", tier=None, run_id=run_id))

        stored = _submission(run_id)
        conn = _ledger()
        slots = [r[0] for r in conn.execute("SELECT state FROM pr_slots")]
        conn.close()
        assert code == 1
        assert stored["remote_write_intent"] is False and stored["repositories"] == []
        assert {r: offline_remotes.branches(r) for r in offline_remotes.bare} == {
            r: ["main"] for r in offline_remotes.bare
        }
        assert offline_remotes.gh_calls() == []
        assert slots == ["released", "released"]

    @pytest.mark.parametrize("change", ["lease-replaced", "attempt-no-longer-writing"])
    def test_ownership_lost_after_the_first_push_stops_every_later_remote_step(
        self, offline_remotes: OfflineRemotes, monkeypatch: pytest.MonkeyPatch, change: str
    ) -> None:
        run_id = _approved(default_store_dir(), "x = 1\n")

        def lose_ownership() -> None:
            if change == "lease-replaced":
                lease = default_store_dir() / "controls" / "leases" / "0.lock"
                lease.unlink()
                lease.write_bytes(b"")
            else:
                conn = _ledger()
                conn.execute("UPDATE publication_attempts SET state = 'failed-after-write'")
                conn.commit()
                conn.close()

        _after_first_push(monkeypatch, lose_ownership)

        code = run_submit(SimpleNamespace(algorithm="selection", tier=None, run_id=run_id))

        stored = _submission(run_id)
        assert code == 1
        assert offline_remotes.gh_calls() == []
        assert f"apprentice/{run_id}" not in offline_remotes.branches("no-magic-ai/no-magic-viz")
        assert stored["status"] == "partial" and stored["remote_write_intent"] is True
        assert [r["pushed"] for r in stored["repositories"]] == [True, False]


class TestRouteAdmission:
    def _status_then_suggest(
        self, tmp_path: Path, prepare: Any, **limits: Any
    ) -> tuple[dict[str, Any], int, list[str], str | None]:
        from apprentice.cli import main

        fixture = ResponsesFixture(outputs={"discovery": "[]"})
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
                **limits,
            )
            held = prepare(config)
            try:
                before = len(fixture.paths())
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    main(["--config", str(config), "status"])
                    code = main(["--config", str(config), "suggest", "--limit", "1"])
            finally:
                if held is not None:
                    held.finish("completed")
            route = _json_objects(buffer.getvalue())[0]["route"]
            denial = _json_objects(buffer.getvalue())[-1].get("control")
            return route, code, fixture.paths()[before:], denial

    @staticmethod
    def _one_suggest(config: Path) -> None:
        from apprentice.cli import main

        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["--config", str(config), "suggest", "--limit", "1"]) == 0

    @staticmethod
    def _live_item(config: Path) -> Any:
        authority = Authority.open(SessionStore().store_dir, _EMPTY)
        return authority.begin_cycle("build", ControlPolicy.from_config(load_config(config)))

    @staticmethod
    def _open_circuit(config: Path) -> None:
        Authority.open(SessionStore().store_dir, _EMPTY).close()
        conn = _ledger()
        until = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
        conn.execute("UPDATE meta SET value = 'open' WHERE key = 'circuit_state'")
        conn.execute("UPDATE meta SET value = ? WHERE key = 'circuit_open_until'", (until,))
        conn.commit()
        conn.close()

    @pytest.mark.parametrize(
        ("blocker", "limits"),
        [
            ("rate_limits.max_concurrent_items", {}),
            ("rate_limits.cooldown_hours", {"cooldown_hours": 4}),
            ("circuit_breaker.failure_threshold", {}),
        ],
    )
    def test_blocked_item_cooldown_or_circuit_is_not_admissible_and_generation_is_denied(
        self, tmp_path: Path, blocker: str, limits: dict[str, Any]
    ) -> None:
        prepare = {
            "rate_limits.max_concurrent_items": self._live_item,
            "rate_limits.cooldown_hours": self._one_suggest,
            "circuit_breaker.failure_threshold": self._open_circuit,
        }[blocker]

        route, code, sent, denial = self._status_then_suggest(tmp_path, prepare, **limits)

        assert route["admissible"] is False
        assert blocker in [b["control"] for b in route["blocked_by"]]
        assert (code, sent, denial) == (1, [], blocker)

    def test_eligible_route_is_admissible_and_generation_is_counted_then_sent(
        self, tmp_path: Path
    ) -> None:
        route, code, sent, _denial = self._status_then_suggest(tmp_path, self._one_suggest)

        assert (route["admissible"], route["blocked_by"]) == (True, [])
        assert code == 0
        assert sent == ["/v1/responses/input_tokens", "/v1/responses"]


class TestRepeatedRollbackPreparation:
    def test_second_preparation_keeps_the_first_suspension_and_its_whole_interval(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        first = installation.prepare_rollback("first@example.invalid")
        first_month = clock.now.strftime("%Y-%m")
        clock.advance(timedelta(days=40))

        second = installation.prepare_rollback("second@example.invalid")
        clock.advance(timedelta(days=40))
        restored = installation.adopt_legacy("operator@example.invalid")

        months = [m["month"] for m in installation.status()["unknown_months"]]
        assert (second["suspended_at"], second["suspended_by"]) == (
            first["suspended_at"],
            "first@example.invalid",
        )
        assert restored["continuity_restored"] is True
        assert months[0] == first_month and len(months) == 3

    def test_first_preparation_starts_the_interval_at_its_own_instant(
        self, tmp_path: Path, installation: Authority, clock: Clock
    ) -> None:
        clock.advance(timedelta(days=40))
        prepared = installation.prepare_rollback("first@example.invalid")

        installation.adopt_legacy("operator@example.invalid")

        months = [m["month"] for m in installation.status()["unknown_months"]]
        assert prepared["suspended_at"] == clock.now.isoformat()
        assert months == [clock.now.strftime("%Y-%m")]


class TestOperatorSignalsAndTheCircuit:
    def _stopped_suggest(self, config: Path, fixture: ResponsesFixture, signum: int) -> int:
        fixture.arrived.clear()
        fixture.gate = threading.Event()
        cli = subprocess.Popen(
            [sys.executable, "-m", "apprentice.cli", "--config", str(config), "suggest"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert fixture.arrived.wait(timeout=60)
            cli.send_signal(signum)
            cli.communicate(timeout=60)
        finally:
            fixture.gate.set()
            fixture.gate = None
            if cli.poll() is None:
                cli.kill()
        return cli.returncode

    def test_operator_terminations_are_neutral_and_an_owner_loss_counts_once(
        self, tmp_path: Path
    ) -> None:
        fixture = ResponsesFixture(outputs={"discovery": "[]"})
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
                failure_threshold=3,
            )
            terms = [self._stopped_suggest(config, fixture, signal.SIGTERM) for _ in range(3)]
            after_terms = Authority.open(default_store_dir(), _EMPTY).status()["circuit"]
            killed = self._stopped_suggest(config, fixture, signal.SIGKILL)
            after_kill = Authority.open(default_store_dir(), _EMPTY).status()["circuit"]

        conn = _ledger()
        outcomes = [r[0] for r in conn.execute("SELECT outcome FROM cycles ORDER BY admitted_at")]
        holds = [
            r[0]
            for r in conn.execute(
                "SELECT state FROM entries WHERE operation = 'generate' ORDER BY created_at"
            )
        ]
        conn.close()
        assert terms == [128 + signal.SIGTERM] * 3 and killed == -signal.SIGKILL
        assert (after_terms["state"], after_terms["consecutive_failures"]) == ("closed", 0)
        assert (after_kill["state"], after_kill["consecutive_failures"]) == ("closed", 1)
        assert outcomes == ["cancelled", "cancelled", "cancelled", "owner-lost"]
        assert holds == ["unknown"] * 4


class TestClaimInstant:
    def test_a_later_admission_reads_an_earlier_claim_and_is_denied_by_the_quota(
        self, offline_remotes: OfflineRemotes, clock: Clock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_dir = default_store_dir()
        first = _approved(store_dir, "x = 1\n")
        second = _approved(store_dir, "y = 2\n")
        # The ledger clock reads this instant for every later decision, while
        # wall time moves on: the first claim is saved after its ledger decision.
        clock.now = datetime.now(tz=UTC)
        config = _limited_config(max_prs_per_day="2", max_prs_per_week="20")
        args = SimpleNamespace(algorithm="selection", tier=None)

        published = run_submit(SimpleNamespace(**vars(args), run_id=first), config)
        capsys.readouterr()
        refused = run_submit(SimpleNamespace(**vars(args), run_id=second), config)

        assert published == 0
        assert refused == 1
        assert (
            _json_objects(capsys.readouterr().out)[-1]["control"] == "rate_limits.max_prs_per_day"
        )
        assert _submission(first)["started_at"] == clock.now.isoformat()
        assert _submission(second) == {}

    def test_a_submission_claimed_in_the_future_still_fails_closed(
        self, offline_remotes: OfflineRemotes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_dir = default_store_dir()
        first = _approved(store_dir, "x = 1\n")
        second = _approved(store_dir, "y = 2\n")
        store = SessionStore(store_dir=store_dir)
        record = store.load(first)
        future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
        record.submission = {"status": "complete", "started_at": future, "repositories": []}
        store.save(record)
        capsys.readouterr()

        code = run_submit(SimpleNamespace(algorithm="selection", tier=None, run_id=second))

        assert code == 1
        assert (
            "control authority unavailable" in _json_objects(capsys.readouterr().out)[-1]["error"]
        )
        assert offline_remotes.gh_calls() == []


def _schema_one_ledger(store_dir: Path, work: Path) -> Path:
    """A schema-1 installation as the previous version leaves it, with rows in every table.

    Same tables (`authority._SCHEMA`), metadata keys and row shapes the
    schema-1 authority writes; the store root and lease directory exist.
    """
    controls = store_dir / "controls"
    (controls / "leases").mkdir(parents=True, mode=0o700)
    store_dir.chmod(0o700)
    controls.chmod(0o700)
    authority_id = "1" * 32
    (controls / "authority.id").write_text(authority_id + "\n")
    database = controls / "accounting.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(authority_module._SCHEMA)
    at = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()
    policy = _policy(work).to_json()
    meta = {
        "schema_version": "1",
        "authority_id": authority_id,
        "created_at": at,
        "last_clock": at,
        "continuity": "continuous",
        "footprint": json.dumps([str(store_dir)]),
        "policy": policy,
    }
    conn.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    conn.execute(
        "INSERT INTO cycles VALUES ('c1', 'build', 'selection-old', 0, ?, ?, 'terminal', "
        "'owner-lost', 'the cycle''s process ended', ?)",
        (at, policy, at),
    )
    conn.execute(
        "INSERT INTO entries VALUES ('e1', 'c1', 'a1', ?, 'implementation', 'drafter', "
        "'generate', 'zero-hosted', ?, 'unknown', 5974, 0, NULL, NULL, '{}', ?, ?)",
        (at[:7], "p" * 64, at, at),
    )
    conn.execute("INSERT INTO quarantine VALUES (?, 'above bound', 'e1', ?)", ("q" * 64, at))
    conn.execute("INSERT INTO unknown_months VALUES (?, 'earlier state', ?)", (at[:7], at))
    conn.execute(
        "INSERT INTO known_records VALUES ('selection-old', 'legacy', 0, ?, ?, 'operator')",
        (at, at),
    )
    conn.commit()
    conn.close()
    return database


def _rows(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = sqlite3.connect(database)
    try:
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall())
            for table in ("cycles", "entries", "quarantine", "unknown_months", "known_records")
        }
    finally:
        conn.close()


def _meta(database: Path) -> dict[str, str]:
    conn = sqlite3.connect(database)
    try:
        return dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()


def _readonly(database: Path) -> None:
    database.chmod(0o400)


def _trigger(database: Path) -> None:
    conn = sqlite3.connect(database)
    conn.execute(
        "CREATE TRIGGER refuse_upgrade BEFORE UPDATE ON meta "
        "BEGIN SELECT RAISE(ABORT, 'injected failure inside the upgrade'); END"
    )
    conn.commit()
    conn.close()


def _child_table_present(database: Path) -> None:
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE publication_attempts (attempt_id TEXT)")
    conn.commit()
    conn.close()


def _damaged_schema_one(database: Path) -> None:
    conn = sqlite3.connect(database)
    conn.execute("UPDATE meta SET value = 'yesterday' WHERE key = 'last_clock'")
    conn.commit()
    conn.close()


def _corrupt_page(database: Path) -> None:
    data = bytearray(database.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    data[page_size : page_size + 64] = b"\xff" * 64
    database.write_bytes(bytes(data))


class TestSchemaOneMigration:
    def test_valid_schema_one_ledger_is_migrated_keeping_every_row_and_value(
        self, tmp_path: Path
    ) -> None:
        database = _schema_one_ledger(tmp_path / "store", tmp_path)
        rows, meta = _rows(database), _meta(database)

        Authority.open(tmp_path / "store", _EMPTY).close()

        migrated = _meta(database)
        assert _rows(database) == rows
        assert migrated.pop("schema_version") == "3"
        assert {k: v for k, v in migrated.items() if k in meta} == {
            k: v for k, v in meta.items() if k != "schema_version"
        }
        assert migrated["circuit_state"] == "closed"

    @pytest.mark.parametrize(
        "fault",
        [_readonly, _trigger, _child_table_present, _corrupt_page, _damaged_schema_one],
        ids=[
            "read-only-file",
            "failure-inside-the-upgrade",
            "unexpected-table",
            "corrupt-page",
            "damaged-schema-one",
        ],
    )
    def test_failed_migration_is_reported_before_any_request_and_left_at_schema_one(
        self, tmp_path: Path, fault: Any
    ) -> None:
        from apprentice.cli import main

        database = _schema_one_ledger(default_store_dir(), tmp_path)
        fault(database)
        before = database.read_bytes()
        fixture = ResponsesFixture(outputs={"discovery": "[]"})
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
            )
            buffer, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
                codes = [main(["--config", str(config), c]) for c in ("status", "suggest")]

        outputs = _json_objects(buffer.getvalue())
        assert codes == [1, 1]
        assert all("control authority unavailable" in out["error"] for out in outputs)
        assert "Traceback" not in errors.getvalue()
        assert fixture.paths() == []
        assert database.read_bytes() == before


def _set(key: str, value: str | None) -> Any:
    def damage(database: Path) -> None:
        conn = sqlite3.connect(database)
        if value is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            conn.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
        conn.commit()
        conn.close()

    return damage


def _sql(statement: str) -> Any:
    def damage(database: Path) -> None:
        conn = sqlite3.connect(database)
        conn.execute(statement)
        conn.commit()
        conn.close()

    return damage


class TestChildLedgerIntegrity:
    @pytest.mark.parametrize(
        "damage",
        [
            _set("circuit_state", None),
            _set("circuit_state", "ajar"),
            _set("circuit_failures", "-1"),
            _set("circuit_open_streak", None),
            _set("circuit_open_until", "soon"),
            _set("circuit_probe", "f" * 32),
            _set("cooldown_anchor", "2026-10-08T00:00:00"),
            _set("window_fill_at", None),
            _set("continuity", "suspended"),
            _sql("DROP TABLE pr_slots"),
            _sql("ALTER TABLE publication_attempts DROP COLUMN detail"),
            _sql("CREATE TABLE extra (x TEXT)"),
        ],
        ids=[
            "missing-circuit-state",
            "unknown-circuit-state",
            "garbled-failure-count",
            "missing-open-streak",
            "garbled-open-deadline",
            "unknown-probe-cycle",
            "naive-cooldown-anchor",
            "missing-window-fill",
            "suspended-without-record",
            "dropped-slots-table",
            "dropped-attempt-column",
            "unexpected-table",
        ],
    )
    def test_damaged_publication_or_circuit_state_is_refused_before_any_request(
        self, tmp_path: Path, damage: Any
    ) -> None:
        from apprentice.cli import main

        fixture = ResponsesFixture(outputs={"discovery": "[]"})
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                assert main(["--config", str(config), "suggest", "--limit", "1"]) == 0
            database = default_store_dir() / "controls" / "accounting.sqlite3"
            damage(database)
            before, sent = database.read_bytes(), fixture.paths()
            with contextlib.redirect_stdout(buffer):
                codes = [main(["--config", str(config), c]) for c in ("status", "suggest")]

        outputs = _json_objects(buffer.getvalue())[-2:]
        assert codes == [1, 1]
        assert all("control authority unavailable" in out["error"] for out in outputs)
        assert fixture.paths() == sent
        assert database.read_bytes() == before


def _pr_urls(submission: dict[str, Any]) -> list[str | None]:
    return [repository["pr_url"] for repository in submission["repositories"]]


class TestKnownRemoteOutcome:
    @pytest.mark.parametrize("fault", ["attempt-end", "cycle-end"])
    def test_ledger_failing_after_both_prs_keeps_and_reports_every_known_effect(
        self,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        fault: str,
    ) -> None:
        from apprentice.controls.errors import AuthorityError

        run_id = _approved(default_store_dir(), "x = 1\n")

        def failing(*args: Any, **kwargs: Any) -> None:
            raise AuthorityError("injected: the ledger cannot record this end")

        args = SimpleNamespace(algorithm="selection", tier=None, run_id=run_id)
        capsys.readouterr()
        with monkeypatch.context() as patch:
            if fault == "attempt-end":
                patch.setattr(Authority, "finish_publication", failing)
            else:
                patch.setattr(authority_module.Cycle, "finish", failing)

            code = run_submit(args)

        reported = _json_objects(capsys.readouterr().out)[-1]
        stored = _submission(run_id)
        urls = [
            "https://github.com/no-magic-ai/no-magic/pull/offline-1",
            "https://github.com/no-magic-ai/no-magic-viz/pull/offline-2",
        ]
        assert code == 1
        assert reported["error"].startswith("control authority unavailable")
        assert _pr_urls(reported) == urls
        assert (stored["status"], stored["remote_write_intent"], _pr_urls(stored)) == (
            "complete",
            True,
            urls,
        )
        conn = _ledger()
        slots = [r[0] for r in conn.execute("SELECT state FROM pr_slots")]
        conn.close()
        assert run_submit(args) == 1
        assert len(offline_remotes.gh_calls()) == 2
        assert slots == ["used", "used"]

    def test_unreadable_intent_after_a_failed_pull_request_keeps_known_effects_unproven(
        self,
        offline_remotes: OfflineRemotes,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from apprentice.controls.errors import AuthorityError

        run_id = _approved(default_store_dir(), "x = 1\n")

        def unreadable(*args: Any, **kwargs: Any) -> None:
            raise AuthorityError("injected: the attempt cannot be read")

        args = SimpleNamespace(algorithm="selection", tier=None, run_id=run_id)
        capsys.readouterr()
        with monkeypatch.context() as patch:
            patch.setattr(Authority, "publication_attempt", unreadable)
            patch.setenv("OFFLINE_GH_FAIL_ON", "1")

            code = run_submit(args)

        reported = _json_objects(capsys.readouterr().out)[-1]
        stored = _submission(run_id)
        assert code == 1
        assert any("control authority unavailable" in e for e in stored["authority_errors"])
        assert stored["remote_write_intent"] is True and stored["status"] == "partial"
        assert [r["pushed"] for r in stored["repositories"]] == [True, True]
        assert reported["repositories"] == stored["repositories"]
        assert run_submit(args) == 1
        assert len(offline_remotes.gh_calls()) == 1


class TestStoredCyclePolicyBounds:
    def test_a_dead_cycle_stored_with_an_unrepresentable_delay_fails_closed_and_keeps_everything(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from apprentice.cli import main

        fixture = ResponsesFixture(outputs={"discovery": "[]"})
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
            )
            assert main(["--config", str(config), "status"]) == 0
            authority = Authority.open(default_store_dir(), _EMPTY)
            cycle = authority.begin_cycle("build", ControlPolicy.from_config(load_config(config)))
            conn = _ledger()
            (stored,) = conn.execute("SELECT policy FROM cycles").fetchone()
            conn.execute(
                "UPDATE cycles SET policy = ?",
                (json.dumps({**json.loads(stored), "half_open_probe_after_minutes": "5e9"}),),
            )
            conn.commit()
            conn.close()
            os.close(cycle._lease_fd)  # the owner is gone; recovery must settle the cycle
            cycle._lease_fd = None
            authority.close()
            database = default_store_dir() / "controls" / "accounting.sqlite3"
            before = database.read_bytes()
            capsys.readouterr()
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                codes = [
                    main(["--config", str(config), c]) for c in ("status", "suggest", "metrics")
                ]

        outputs = _json_objects(capsys.readouterr().out)
        assert codes == [1, 1, 1]
        assert all("control authority unavailable" in out["error"] for out in outputs)
        assert "Traceback" not in errors.getvalue()
        assert fixture.paths() == []
        assert database.read_bytes() == before


class TestOperatorStopAndTheCircuit:
    @pytest.mark.parametrize(
        ("method", "failing", "failures"),
        [("complete_run", False, 0), ("fail_run", True, 1)],
    )
    @pytest.mark.usefixtures("judged_execution")
    def test_stop_while_recording_the_end_never_turns_the_outcome_into_an_owner_loss(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        failing: bool,
        failures: int,
    ) -> None:
        from apprentice.cli import main
        from tests.conftest import fixture_outputs
        from tests.test_cli import _stop_at

        # A completing build runs three parallel 15,000-token artifact roles; under the default
        # 20,000-token stage the role admitted last gets only the residual cap and its truncated
        # output halts the build before complete_run, so that case gets an adequate stage.
        stage = {} if failing else {"max_tokens_per_stage": 100_000}
        fixture = ResponsesFixture(outputs=fixture_outputs(failing_implementation=failing))
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
                failure_threshold=3,
                **stage,
            )
            _stop_at(monkeypatch, method, "before")
            code = main(["--config", str(config), "build", "selection"])
            circuit = Authority.open(default_store_dir(), _EMPTY).status()["circuit"]

        conn = _ledger()
        outcomes = [r[0] for r in conn.execute("SELECT outcome FROM cycles")]
        conn.close()
        assert code == 128 + signal.SIGTERM
        assert outcomes == ["failed" if failing else "completed"]
        assert (circuit["state"], circuit["consecutive_failures"]) == ("closed", failures)


class TestUnsavableRunRecordAndTheCircuit:
    """A build's run record cannot be written at its end (a real EACCES at the record writer)
    while the control ledger stays writable; the next command's recovery must not count it again."""

    @pytest.mark.parametrize(
        ("ending", "outcome", "failures"),
        [
            ("operator-stop", "cancelled", 0),
            ("failing-build", "failed", 1),
            ("stop-while-recording-a-failed-end", "failed", 1),
        ],
    )
    @pytest.mark.usefixtures("judged_execution")
    def test_the_cycle_ends_as_reached_and_counts_toward_the_circuit_at_most_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ending: str,
        outcome: str,
        failures: int,
    ) -> None:
        from apprentice.cli import main
        from tests.conftest import fixture_outputs

        failing = ending != "operator-stop"
        fixture = ResponsesFixture(outputs=fixture_outputs(failing_implementation=failing))
        store = default_store_dir()
        with ResponsesServer(fixture) as server:
            config = write_config(
                tmp_path / "apprentice.toml",
                profile=local_profile(tmp_path / "profile.json"),
                base_url=server.base_url,
                failure_threshold=3,
            )
            assert main(["--config", str(config), "status"]) == 0
            original = store.stat().st_mode & 0o777
            if ending == "operator-stop":
                respond = fixture.respond

                def stop_read_only(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
                    if path == "/v1/responses":
                        fixture.respond = respond  # type: ignore[method-assign]
                        store.chmod(0o500)
                        os.kill(os.getpid(), signal.SIGTERM)
                    return respond(path, body)

                fixture.respond = stop_read_only  # type: ignore[method-assign]
            else:
                write = SessionStore._write

                def write_read_only(self: SessionStore, record: Any) -> None:
                    store.chmod(0o500)
                    if ending == "stop-while-recording-a-failed-end":
                        os.kill(os.getpid(), signal.SIGTERM)
                    write(self, record)

                monkeypatch.setattr(SessionStore, "_write", write_read_only)
            try:
                code = main(["--config", str(config), "build", "selection"])
            finally:
                store.chmod(original)
            circuits = []
            for _next_command in range(2):
                authority = Authority.open(store, _EMPTY)
                circuits.append(authority.status()["circuit"]["consecutive_failures"])
                authority.close()

        conn = _ledger()
        cycles = conn.execute("SELECT state, outcome FROM cycles WHERE kind = 'build'").fetchall()
        conn.close()
        (record,) = SessionStore(store_dir=store).list_runs(limit=None)
        assert code == (1 if ending == "failing-build" else 128 + signal.SIGTERM)
        assert cycles == [("terminal", outcome)]
        assert circuits == [failures, failures]
        assert record.status == "in_progress"


def _open_connections() -> int:
    import gc

    count = 0
    for candidate in gc.get_objects():
        if isinstance(candidate, sqlite3.Connection):
            try:
                candidate.total_changes  # noqa: B018 - raises once the connection is closed
            except sqlite3.ProgrammingError:
                continue
            count += 1
    return count


class TestInterruptedUpgrade:
    @pytest.mark.parametrize(
        "fault", ["read-only-file", "held-write-lock", "failure-inside-the-upgrade"]
    )
    def test_valid_ledger_whose_upgrade_did_not_complete_is_unchanged_and_upgrades_later(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
    ) -> None:
        from apprentice.controls.errors import AuthorityError

        # A held lock is waited for this long here instead of the shipped 60 s.
        monkeypatch.setattr(authority_module, "BUSY_TIMEOUT_SECONDS", 1)
        store = tmp_path / "store"
        database = _schema_one_ledger(store, tmp_path)
        rows = _rows(database)
        before = database.read_bytes()
        holder: sqlite3.Connection | None = None
        if fault == "read-only-file":
            _readonly(database)
        elif fault == "held-write-lock":
            holder = sqlite3.connect(database, isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")
        else:
            _trigger(database)
            before = database.read_bytes()
        connections = _open_connections()

        with pytest.raises(AuthorityError) as failed:
            Authority.open(store, _EMPTY)

        leaked = _open_connections() - connections
        if holder is not None:
            holder.rollback()
            holder.close()
        assert database.read_bytes() == before
        assert _meta(database)["schema_version"] == "1"
        assert leaked == 0
        assert "unchanged" in str(failed.value) and "restore it from backup" not in str(
            failed.value
        )
        if fault == "read-only-file":
            database.chmod(0o600)
        elif fault == "failure-inside-the-upgrade":
            _sql("DROP TRIGGER refuse_upgrade")(database)

        Authority.open(store, _EMPTY).close()

        assert _rows(database) == rows
        assert _meta(database)["schema_version"] == "3"

    def test_damaged_schema_one_ledger_is_never_upgraded_and_names_a_restore(
        self, tmp_path: Path
    ) -> None:
        from apprentice.controls.errors import AuthorityError

        store = tmp_path / "store"
        database = _schema_one_ledger(store, tmp_path)
        _damaged_schema_one(database)
        before = database.read_bytes()

        for _attempt in range(2):
            with pytest.raises(AuthorityError) as refused:
                Authority.open(store, _EMPTY)
            assert "restore it from backup" in str(refused.value)

        assert database.read_bytes() == before
        assert _meta(database)["schema_version"] == "1"


def _run_record(
    store_dir: Path, run_id: str, started: datetime, submission: dict[str, Any]
) -> None:
    (store_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "algorithm_name": "selection",
                "tier": 2,
                "status": "completed",
                "started_at": started.isoformat(),
                "completed_at": started.isoformat(),
                "submission": submission,
            }
        )
    )


def _ledger_less_submission(started: datetime) -> dict[str, Any]:
    return {
        "status": "complete",
        "started_at": started.isoformat(),
        "repositories": [
            {"repository": r, "pushed": True, "pr_url": f"https://github.com/{r}/pull/older"}
            for r in ("no-magic-ai/no-magic", "no-magic-ai/no-magic-viz")
        ],
    }


_OWNED = "selection-20261001T000000Z-" + "a" * 32
_BOOKED = "selection-20261001T000000Z-" + "b" * 32
_REPLACED = "selection-20261001T000000Z-" + "c" * 32
_GONE = "selection-20261001T000000Z-" + "d" * 32


def _schema_two_ledger(store_dir: Path, work: Path) -> Path:
    """A schema-2 installation exactly as the first version of these controls defined it.

    Tables are `authority._SCHEMA` + `publication.HISTORICAL_SCHEMA_V2` (the
    first definition, byte-identical); it holds an owned complete and an owned
    written-nothing attempt with their slots, a claim marker, a registered
    foreign submission with its legacy slots, a registration whose submission
    was later replaced, one whose record is gone, an open circuit with its
    deadline and window/cooldown holds — plus run records of those runs.
    """
    from apprentice.controls import limits, publication

    database = _schema_one_ledger(store_dir, work)
    conn = sqlite3.connect(database)
    conn.executescript(publication.HISTORICAL_SCHEMA_V2)
    now = datetime.now(tz=UTC)
    t0, t1, t2 = (now - timedelta(hours=h) for h in (3, 2, 1))
    circuit = {
        **limits.CIRCUIT_DEFAULTS,
        "circuit_state": "open",
        "circuit_open_streak": "1",
        "circuit_open_until": (now + timedelta(minutes=30)).isoformat(),
        "cooldown_anchor": t1.isoformat(),
        "window_fill_at": t1.isoformat(),
    }
    conn.executemany("INSERT INTO meta VALUES (?, ?)", circuit.items())
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    (policy,) = conn.execute("SELECT value FROM meta WHERE key = 'policy'").fetchone()
    for cycle, outcome in (("c-complete", "completed"), ("c-nowrite", "denied")):
        conn.execute(
            "INSERT INTO cycles VALUES (?, 'submit', ?, 1, ?, ?, 'terminal', ?, '', ?)",
            (cycle, _OWNED, t0.isoformat(), policy, outcome, t0.isoformat()),
        )
    for attempt, cycle, state, intent in (
        ("a-complete", "c-complete", "complete", 1),
        ("a-nowrite", "c-nowrite", "ended-before-any-remote-write", 0),
    ):
        conn.execute(
            "INSERT INTO publication_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, '')",
            (
                attempt,
                cycle,
                _OWNED,
                "m" * 64,
                '["no-magic-ai/no-magic", "no-magic-ai/no-magic-viz"]',
                state,
                intent,
                t0.isoformat(),
                t0.isoformat(),
            ),
        )
        for repository in ("no-magic-ai/no-magic", "no-magic-ai/no-magic-viz"):
            conn.execute(
                "INSERT INTO pr_slots VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"{attempt}-{repository}",
                    attempt,
                    _OWNED,
                    repository,
                    t0.isoformat(),
                    "used" if intent else "released",
                ),
            )
    for i in range(2):
        conn.execute(
            "INSERT INTO pr_slots VALUES (?, NULL, ?, 'unknown', ?, 'legacy')",
            (f"legacy-{i}", _BOOKED, t0.isoformat()),
        )
    conn.executemany(
        "INSERT INTO known_submissions VALUES (?, ?)",
        [
            (_OWNED, t0.isoformat()),
            (_BOOKED, t1.isoformat()),
            (_REPLACED, t1.isoformat()),
            (_GONE, t1.isoformat()),
        ],
    )
    conn.commit()
    conn.close()
    _run_record(
        store_dir,
        _OWNED,
        t0,
        {
            "status": "complete",
            "started_at": t0.isoformat(),
            "attempt_id": "a-complete",
            "remote_write_intent": True,
            "repositories": [],
        },
    )
    _run_record(store_dir, _BOOKED, t0, _ledger_less_submission(t0))
    _run_record(store_dir, _REPLACED, t0, _ledger_less_submission(t2))
    return database


def _all_rows(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = sqlite3.connect(database)
    try:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if r[0] != "meta"  # compared separately: only schema_version may change
        ]
        return {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall()) for t in tables}
    finally:
        conn.close()


class TestSchemaTwoMigration:
    def test_every_schema_two_row_is_kept_and_run_rows_become_submission_identities(
        self, tmp_path: Path
    ) -> None:
        store = tmp_path / "store"
        database = _schema_two_ledger(store, tmp_path)
        before, meta = _all_rows(database), _meta(database)

        Authority.open(store, _EMPTY).close()

        after = _all_rows(database)
        known = after.pop("known_submissions")
        old_known = before.pop("known_submissions")
        migrated = _meta(database)
        assert migrated.pop("schema_version") == "3" and meta.pop("schema_version") == "2"
        assert after == before and migrated == meta
        booked_start = next(r for r in old_known if r[0] == _BOOKED)
        assert sorted((identity, run, seen) for identity, run, seen in known) == sorted(
            [
                (
                    f"schema2:{_OWNED}",
                    _OWNED,
                    old_known[[r[0] for r in old_known].index(_OWNED)][1],
                ),
                (
                    "run:"
                    + _BOOKED
                    + "@"
                    + json.loads((store / f"{_BOOKED}.json").read_text())["submission"][
                        "started_at"
                    ],
                    _BOOKED,
                    booked_start[1],
                ),
                (
                    f"schema2:{_REPLACED}",
                    _REPLACED,
                    old_known[[r[0] for r in old_known].index(_REPLACED)][1],
                ),
                (f"schema2:{_GONE}", _GONE, old_known[[r[0] for r in old_known].index(_GONE)][1]),
            ]
        )

    @pytest.mark.parametrize(
        ("published", "run_started"),
        [(timedelta(minutes=30), timedelta(hours=3)), (timedelta(hours=4), timedelta(hours=5))],
        ids=["after-the-claim-marker", "before-the-claim-marker"],
    )
    def test_after_migration_only_unbooked_foreign_submissions_are_counted_exactly_once(
        self, tmp_path: Path, published: timedelta, run_started: timedelta
    ) -> None:
        store = tmp_path / "store"
        _schema_two_ledger(store, tmp_path)
        # The run this ledger claimed (its claim marker was seen 3 hours ago) is
        # published by an earlier apprentice; whenever that submission started,
        # the marker is not its booking.
        now = datetime.now(tz=UTC)
        _run_record(store, _OWNED, now - run_started, _ledger_less_submission(now - published))

        for _restart in range(2):
            authority = Authority.open(store, _EMPTY)
            authority.status()
            authority.close()

        conn = sqlite3.connect(store / "controls" / "accounting.sqlite3")
        legacy = dict(
            conn.execute(
                "SELECT run_id, COUNT(*) FROM pr_slots WHERE state = 'legacy' GROUP BY run_id"
            )
        )
        owned = dict(
            conn.execute(
                "SELECT state, COUNT(*) FROM pr_slots WHERE attempt_id IS NOT NULL GROUP BY state"
            )
        )
        conn.close()
        assert legacy == {_BOOKED: 2, _REPLACED: 2, _OWNED: 2}
        assert owned == {"used": 2, "released": 2}

    def test_two_booked_foreign_runs_sharing_one_attempt_id_each_keep_their_booking(
        self, tmp_path: Path
    ) -> None:
        store = tmp_path / "store"
        database = _schema_two_ledger(store, tmp_path)
        now = datetime.now(tz=UTC)
        started, seen = now - timedelta(hours=2), now - timedelta(hours=1)
        sharing = ["selection-20261001T000000Z-" + c * 32 for c in "ef"]
        conn = sqlite3.connect(database)
        for run_id in sharing:
            conn.execute("INSERT INTO known_submissions VALUES (?, ?)", (run_id, seen.isoformat()))
            for slot in range(2):
                conn.execute(
                    "INSERT INTO pr_slots VALUES (?, NULL, ?, 'unknown', ?, 'legacy')",
                    (f"{run_id}-{slot}", run_id, started.isoformat()),
                )
        conn.commit()
        conn.close()
        for run_id in sharing:
            submission = {**_ledger_less_submission(started), "attempt_id": "9" * 32}
            _run_record(store, run_id, started, submission)
        before, meta = _all_rows(database), _meta(database)
        before.pop("known_submissions")

        Authority.open(store, _EMPTY).close()
        migrated, migrated_meta = _all_rows(database), _meta(database)
        for _restart in range(2):
            authority = Authority.open(store, _EMPTY)
            authority.status()
            authority.close()

        known = {
            run: (identity, seen_at)
            for identity, run, seen_at in migrated.pop("known_submissions")
            if run in sharing
        }
        assert (migrated_meta.pop("schema_version"), meta.pop("schema_version")) == ("3", "2")
        assert migrated_meta == meta
        assert known == {
            sharing[0]: ("attempt:" + "9" * 32, seen.isoformat()),
            sharing[1]: (f"run:{sharing[1]}@{started.isoformat()}", seen.isoformat()),
        }
        assert migrated == before
        booked = [slot for slot in before["pr_slots"] if slot[2] in sharing]
        assert [slot for slot in _all_rows(database)["pr_slots"] if slot[2] in sharing] == booked

    def test_current_ledger_reopens_unchanged(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        database = _schema_two_ledger(store, tmp_path)
        Authority.open(store, _EMPTY).close()
        migrated = database.read_bytes()

        Authority.open(store, _EMPTY).close()

        assert database.read_bytes() == migrated
        assert _meta(database)["schema_version"] == "3"

    @pytest.mark.parametrize(
        "damage",
        [
            _sql("UPDATE meta SET value = 'ajar' WHERE key = 'circuit_state'"),
            _sql("ALTER TABLE known_submissions ADD COLUMN identity TEXT"),
            _sql("DROP TABLE known_submissions"),
        ],
        ids=["garbled-circuit-state", "altered-known-submissions", "missing-known-submissions"],
    )
    def test_damaged_schema_two_ledger_is_refused_unchanged_and_never_upgraded(
        self, tmp_path: Path, damage: Any
    ) -> None:
        from apprentice.controls.errors import AuthorityError

        store = tmp_path / "store"
        database = _schema_two_ledger(store, tmp_path)
        damage(database)
        before = database.read_bytes()

        with pytest.raises(AuthorityError) as refused:
            Authority.open(store, _EMPTY)

        assert "restore it from backup" in str(refused.value)
        assert database.read_bytes() == before
        assert _meta(database)["schema_version"] == "2"

    def test_schema_two_with_the_schema_three_shape_is_not_accepted(self, tmp_path: Path) -> None:
        from apprentice.controls import publication
        from apprentice.controls.errors import AuthorityError

        store = tmp_path / "store"
        database = _schema_two_ledger(store, tmp_path)
        _sql("DROP TABLE known_submissions")(database)
        _sql(publication.KNOWN_SUBMISSIONS_V3)(database)
        before = database.read_bytes()

        with pytest.raises(AuthorityError):
            Authority.open(store, _EMPTY)

        assert database.read_bytes() == before

    @pytest.mark.parametrize(
        "fault", ["read-only-file", "held-write-lock", "failure-inside-the-upgrade"]
    )
    def test_interrupted_schema_two_upgrade_keeps_schema_two_and_upgrades_later(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
    ) -> None:
        from apprentice.controls.errors import AuthorityError

        monkeypatch.setattr(authority_module, "BUSY_TIMEOUT_SECONDS", 1)
        store = tmp_path / "store"
        database = _schema_two_ledger(store, tmp_path)
        rows = _all_rows(database)
        holder: sqlite3.Connection | None = None
        if fault == "read-only-file":
            _readonly(database)
        elif fault == "held-write-lock":
            before = database.read_bytes()
            holder = sqlite3.connect(database, isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")
        else:
            # Fails the upgrade's last statement, after every table was rebuilt.
            _sql(
                "CREATE TRIGGER refuse_drop BEFORE UPDATE ON meta "
                "BEGIN SELECT RAISE(ABORT, 'injected failure inside the upgrade'); END"
            )(database)
        if holder is None:
            before = database.read_bytes()
        connections = _open_connections()

        with pytest.raises(AuthorityError) as failed:
            Authority.open(store, _EMPTY)

        leaked = _open_connections() - connections
        if holder is not None:
            holder.rollback()
            holder.close()
        assert database.read_bytes() == before
        assert _meta(database)["schema_version"] == "2"
        assert leaked == 0
        assert "unchanged (still schema 2)" in str(failed.value)
        if fault == "read-only-file":
            database.chmod(0o600)
        elif fault == "failure-inside-the-upgrade":
            _sql("DROP TRIGGER refuse_drop")(database)

        Authority.open(store, _EMPTY).close()

        after = _all_rows(database)
        assert _meta(database)["schema_version"] == "3"
        assert {t: v for t, v in after.items() if t != "known_submissions"} == {
            t: v for t, v in rows.items() if t != "known_submissions"
        }
        assert len(after["known_submissions"]) == len(rows["known_submissions"])
