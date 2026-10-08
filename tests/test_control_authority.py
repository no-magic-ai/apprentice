"""Durable control authority: fail-closed state, earlier footprints, owner loss and policy."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from google.adk.models import LlmRequest
from google.genai import types

from apprentice.cli import main
from apprentice.controls.authority import Authority
from apprentice.controls.errors import AuthorityError, ControlDeniedError
from apprentice.controls.footprint import Footprint, capture_footprint
from apprentice.controls.policy import ControlPolicy
from apprentice.core.config import load_config
from apprentice.core.session_store import SessionStore, default_store_dir
from apprentice.providers.factory import resolve_route
from tests.responses_fixture import ResponsesFixture, ResponsesServer, local_profile, write_config

_EMPTY = Footprint(existing=())


@pytest.fixture
def server() -> Any:
    with ResponsesServer(ResponsesFixture(outputs={"discovery": "[]"})) as running:
        yield running


@pytest.fixture
def config_path(tmp_path: Path, server: ResponsesServer) -> Path:
    return write_config(
        tmp_path / "apprentice.toml",
        profile=local_profile(tmp_path / "profile.json"),
        base_url=server.base_url,
    )


def _policy(config_path: Path) -> ControlPolicy:
    return ControlPolicy.from_config(load_config(config_path))


def _call(config_path: Path, authority: Authority) -> None:
    config = load_config(config_path)
    route = resolve_route(config.provider)
    request = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text="Suggest one.")])],
        config=types.GenerateContentConfig(system_instruction="You are a curriculum designer."),
    )

    async def run(model: Any) -> None:
        async for _ in model.generate_content_async(request):
            pass

    with authority.begin_cycle("suggest", ControlPolicy.from_config(config)) as cycle:
        asyncio.run(run(route.model(cycle, "discovery", "discovery")))


def _json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects, index = [], 0
    while index < len(text.rstrip()):
        while text[index].isspace():
            index += 1
        value, index = decoder.raw_decode(text, index)
        objects.append(value)
    return objects


def _write_record(store_dir: Path, run_id: str, status: str, started: datetime) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "algorithm_name": "selection",
                "tier": 2,
                "status": status,
                "started_at": started.isoformat(),
            }
        )
    )


class TestFailClosedState:
    def test_marker_without_ledger_is_fatal_and_never_recreated(self, tmp_path: Path) -> None:
        Authority.open(tmp_path, _EMPTY).close()
        (tmp_path / "controls" / "accounting.sqlite3").unlink()

        with pytest.raises(AuthorityError, match="partial"):
            Authority.open(tmp_path, _EMPTY)

        assert not (tmp_path / "controls" / "accounting.sqlite3").exists()

    def test_ledger_without_marker_is_fatal(self, tmp_path: Path) -> None:
        Authority.open(tmp_path, _EMPTY).close()
        (tmp_path / "controls" / "authority.id").unlink()

        with pytest.raises(AuthorityError, match="partial"):
            Authority.open(tmp_path, _EMPTY)

    def test_foreign_marker_is_fatal(self, tmp_path: Path) -> None:
        Authority.open(tmp_path, _EMPTY).close()
        (tmp_path / "controls" / "authority.id").write_text("someone-else\n")

        with pytest.raises(AuthorityError, match="another authority"):
            Authority.open(tmp_path, _EMPTY)

    def test_corrupt_ledger_is_fatal(self, tmp_path: Path) -> None:
        Authority.open(tmp_path, _EMPTY).close()
        (tmp_path / "controls" / "accounting.sqlite3").write_bytes(b"not a database" * 100)

        with pytest.raises(AuthorityError):
            Authority.open(tmp_path, _EMPTY)

    def test_clock_earlier_than_the_last_decision_is_fatal(
        self, tmp_path: Path, config_path: Path
    ) -> None:
        authority = Authority.open(tmp_path, _EMPTY)
        future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
        with authority.transaction() as (conn, _now):
            conn.execute("UPDATE meta SET value = ? WHERE key = 'last_clock'", (future,))

        with pytest.raises(AuthorityError, match="earlier than the ledger"):
            authority.begin_cycle("suggest", _policy(config_path))


class TestEarlierInstallationState:
    def test_default_store_and_log_roots_count_as_footprint(self, private_home: Path) -> None:
        (private_home / ".apprentice" / "logs").mkdir(parents=True)

        footprint = capture_footprint(private_home / "elsewhere", private_home / "other-logs")

        assert footprint.existing == (str(private_home / ".apprentice" / "logs"),)

    def test_earlier_footprint_holds_the_current_month_unknown(
        self, tmp_path: Path, config_path: Path, server: ResponsesServer
    ) -> None:
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        authority = Authority.open(store_dir, Footprint(existing=(str(store_dir),)))

        with pytest.raises(ControlDeniedError, match="unknown and held") as denied:
            _call(config_path, authority)

        assert denied.value.control == "budget.global"
        assert server.fixture.paths() == []

    def test_live_legacy_record_blocks_until_the_operator_adopts_it(
        self, tmp_path: Path, config_path: Path
    ) -> None:
        store_dir = tmp_path / "store"
        old = datetime(2026, 1, 15, tzinfo=UTC)
        _write_record(store_dir, "selection-20260115T000000Z", "in_progress", old)
        authority = Authority.open(store_dir, Footprint(existing=(str(store_dir),)))

        with pytest.raises(ControlDeniedError) as blocked:
            authority.begin_cycle("suggest", _policy(config_path))
        adopted = authority.adopt_legacy("operator@example.invalid")
        with authority.begin_cycle("suggest", _policy(config_path)):
            pass

        assert blocked.value.control == "controls.legacy"
        assert adopted["adopted_records"] == ["selection-20260115T000000Z"]
        months = [m["month"] for m in authority.status()["unknown_months"]]
        assert months == [datetime.now(tz=UTC).strftime("%Y-%m")]

    def test_record_written_without_the_authority_holds_the_month_on_next_admission(
        self, tmp_path: Path, config_path: Path, server: ResponsesServer
    ) -> None:
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        authority = Authority.open(store_dir, _EMPTY)
        _call(config_path, authority)
        SessionStore(store_dir=store_dir).create_run("selection", 2)

        with pytest.raises(ControlDeniedError, match="written without this authority"):
            _call(config_path, authority)

        assert len(server.fixture.generations()) == 1

    def test_malformed_record_timestamp_fails_closed(
        self, tmp_path: Path, config_path: Path
    ) -> None:
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        authority = Authority.open(store_dir, _EMPTY)
        record = store_dir / "selection-20260101T000000Z.json"
        record.write_text(
            json.dumps(
                {
                    "run_id": "selection-20260101T000000Z",
                    "algorithm_name": "selection",
                    "tier": 2,
                    "status": "failed",
                    "started_at": "2026-01-01T00:00:00",
                }
            )
        )

        with pytest.raises(AuthorityError, match="no timezone"):
            authority.begin_cycle("suggest", _policy(config_path))


class TestOwnerLoss:
    def test_killed_owner_keeps_its_dispatched_bound_unknown_and_frees_its_lease(
        self, tmp_path: Path, config_path: Path, server: ResponsesServer
    ) -> None:
        server.fixture.gate = threading.Event()
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        Authority.open(store_dir, _EMPTY).close()
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).parent / "control_worker.py"),
                str(store_dir),
                str(config_path),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert worker.stdout is not None
            assert worker.stdout.readline().strip() == "ADMITTED"
            assert server.fixture.arrived.wait(timeout=30)
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=30)
        finally:
            if worker.poll() is None:
                worker.kill()

        authority = Authority.open(store_dir, _EMPTY)
        status = authority.status()
        with authority.begin_cycle("suggest", _policy(config_path)) as cycle:
            slot = cycle.authority._conn.execute(
                "SELECT slot FROM cycles WHERE cycle_id = ?", (cycle.cycle_id,)
            ).fetchone()[0]

        conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
        lost = conn.execute(
            "SELECT outcome FROM cycles WHERE kind = 'suggest' ORDER BY admitted_at"
        ).fetchall()
        generation = conn.execute(
            "SELECT state, bound_tokens, charged_tokens FROM entries WHERE operation = 'generate'"
        ).fetchone()
        conn.close()
        assert status["live_cycles"] == []
        assert lost[0] == ("owner-lost",)
        assert generation[0] == "unknown" and generation[2] is None
        assert slot == 0


class TestPolicyAdoption:
    def test_conflicting_policy_waits_for_quiescence_then_tightens_without_erasing(
        self, tmp_path: Path, config_path: Path, server: ResponsesServer
    ) -> None:
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        authority = Authority.open(store_dir, _EMPTY)
        _call(config_path, authority)
        tighter = write_config(
            tmp_path / "tight.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            monthly_token_ceiling=10,
        )

        with (
            authority.begin_cycle("suggest", _policy(config_path)),
            pytest.raises(ControlDeniedError) as conflicting,
        ):
            authority.begin_cycle("suggest", _policy(tighter))
        with pytest.raises(ControlDeniedError) as exhausted:
            _call(tighter, authority)

        assert conflicting.value.control == "controls.policy"
        assert exhausted.value.control == "budget.global.monthly_token_ceiling"
        entries = authority.status()["month_entries"]
        assert sum(e["charged_tokens"] for e in entries) > 10


class TestStatusCommand:
    def test_status_reports_configured_limits_apart_from_ledger_state(
        self, config_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(config_path), "status"]) == 0

        report = json.loads(capsys.readouterr().out)
        assert report["configured"]["max_tokens_per_stage"] == "20000"
        assert report["ledger"]["month_entries"] == []
        assert (report["route"]["structurally_valid"], report["route"]["admissible"]) == (
            True,
            True,
        )

    def test_quarantined_profile_is_valid_in_structure_but_not_admissible(
        self, config_path: Path, server: ResponsesServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def overrun(usage: dict[str, Any]) -> dict[str, Any]:
            usage["output_tokens"] += 10_000
            usage["total_tokens"] += 10_000
            return usage

        server.fixture.usage_patch = overrun
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 1
        capsys.readouterr()

        assert main(["--config", str(config_path), "status"]) == 0

        route = json.loads(capsys.readouterr().out)["route"]
        assert route["structurally_valid"] is True
        assert route["admissible"] is False
        assert [b["control"] for b in route["blocked_by"]] == ["provider.accounting_profile_path"]

    def test_dropped_ledger_table_is_reported_unavailable_and_left_as_found(
        self, config_path: Path, server: ResponsesServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(config_path), "status"]) == 0
        database = default_store_dir() / "controls" / "accounting.sqlite3"
        conn = sqlite3.connect(database)
        conn.execute("DROP TABLE known_records")
        conn.commit()
        conn.close()
        capsys.readouterr()

        codes = [main(["--config", str(config_path), command]) for command in ("status", "suggest")]

        outputs = _json_objects(capsys.readouterr().out)
        assert codes == [1, 1]
        assert all("control authority unavailable" in out["error"] for out in outputs)
        assert server.fixture.paths() == []
        conn = sqlite3.connect(database)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert "known_records" not in tables


class TestFreshInstallation:
    @pytest.mark.parametrize("first", [["config"], ["history"], ["build", "selection"]])
    def test_own_first_command_does_not_hold_the_month_unknown(
        self, tmp_path: Path, server: ResponsesServer, first: list[str]
    ) -> None:
        unprofiled = write_config(
            tmp_path / "unprofiled.toml",
            profile=tmp_path / "missing-profile.json",
            base_url=server.base_url,
            log_path=str(Path.home() / ".apprentice" / "logs"),
        )
        configured = write_config(
            tmp_path / "configured.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            log_path=str(Path.home() / ".apprentice" / "logs"),
        )

        main(["--config", str(unprofiled), *first])
        code = main(["--config", str(configured), "suggest", "--limit", "1"])

        assert code == 0
        assert server.fixture.paths() == ["/v1/responses/input_tokens", "/v1/responses"]

    def test_genuine_earlier_logs_still_hold_the_month_unknown(
        self, tmp_path: Path, server: ResponsesServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (Path.home() / ".apprentice" / "logs").mkdir(parents=True)
        configured = write_config(
            tmp_path / "configured.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            log_path=str(Path.home() / ".apprentice" / "logs"),
        )

        assert main(["--config", str(configured), "suggest", "--limit", "1"]) == 1

        assert json.loads(capsys.readouterr().out)["control"] == "budget.global"
        assert server.fixture.paths() == []


def _damage_pages(database: Path) -> None:
    data = bytearray(database.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    data[page_size : page_size + 64] = b"\xff" * 64
    database.write_bytes(bytes(data))


def _execute(statement: str) -> Any:
    def damage(database: Path) -> None:
        conn = sqlite3.connect(database)
        conn.execute(statement)
        conn.commit()
        conn.close()

    return damage


class TestLedgerIntegrity:
    @pytest.mark.parametrize(
        "damage",
        [
            _execute("DROP TABLE unknown_months"),
            _execute("ALTER TABLE quarantine ADD COLUMN note TEXT"),
            _execute("DELETE FROM meta WHERE key = 'last_clock'"),
            _execute("UPDATE meta SET value = 'yesterday' WHERE key = 'last_clock'"),
            _execute("UPDATE meta SET value = 'paused' WHERE key = 'continuity'"),
            _execute("UPDATE meta SET value = '{' WHERE key = 'policy'"),
            _execute("DELETE FROM meta WHERE key = 'policy'"),
            _damage_pages,
        ],
        ids=[
            "dropped-table",
            "altered-columns",
            "missing-clock",
            "garbled-clock",
            "unknown-continuity",
            "garbled-policy",
            "missing-policy",
            "corrupt-page",
        ],
    )
    def test_damaged_ledger_is_refused_before_any_request_and_left_as_found(
        self,
        config_path: Path,
        server: ResponsesServer,
        capsys: pytest.CaptureFixture[str],
        damage: Any,
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        database = default_store_dir() / "controls" / "accounting.sqlite3"
        damage(database)
        before = database.read_bytes()
        sent = server.fixture.paths()
        capsys.readouterr()

        code = main(["--config", str(config_path), "suggest", "--limit", "1"])

        captured = capsys.readouterr()
        assert code == 1
        assert "control authority unavailable" in _json_objects(captured.out)[-1]["error"]
        assert "Traceback" not in captured.err
        assert server.fixture.paths() == sent
        assert database.read_bytes() == before

    def test_ledger_failing_after_admission_ends_with_an_actionable_error(
        self, config_path: Path, server: ResponsesServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(config_path), "status"]) == 0
        database = default_store_dir() / "controls" / "accounting.sqlite3"
        respond = server.fixture.respond

        def drop_on_count(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if path == "/v1/responses/input_tokens":
                conn = sqlite3.connect(database)
                conn.execute("DROP TABLE entries")
                conn.commit()
                conn.close()
            return respond(path, body)

        server.fixture.respond = drop_on_count  # type: ignore[method-assign]
        capsys.readouterr()

        code = main(["--config", str(config_path), "suggest", "--limit", "1"])

        captured = capsys.readouterr()
        assert code == 1
        assert "control authority unavailable" in _json_objects(captured.out)[-1]["error"]
        assert "Traceback" not in captured.err
        assert server.fixture.paths() == ["/v1/responses/input_tokens"]

    def test_failing_ledger_raises_typed_errors_and_keeps_the_lease_held(
        self, tmp_path: Path, config_path: Path
    ) -> None:
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        authority = Authority.open(store_dir, _EMPTY)
        cycle = authority.begin_cycle("suggest", _policy(config_path))
        conn = sqlite3.connect(store_dir / "controls" / "accounting.sqlite3")
        conn.execute("DROP TABLE entries")
        conn.commit()
        conn.close()

        with pytest.raises(AuthorityError, match="unreadable"):
            cycle.summary()
        with pytest.raises(AuthorityError):
            cycle.finish("completed")

        lease = os.open(store_dir / "controls" / "leases" / "0.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lease)

    @pytest.mark.parametrize("kept", ["authority.id", "accounting.sqlite3"])
    def test_partial_installation_is_refused_by_the_cli_and_never_completed(
        self,
        config_path: Path,
        server: ResponsesServer,
        capsys: pytest.CaptureFixture[str],
        kept: str,
    ) -> None:
        assert main(["--config", str(config_path), "status"]) == 0
        controls = default_store_dir() / "controls"
        missing = {"authority.id", "accounting.sqlite3"} - {kept}
        (controls / missing.pop()).unlink()
        before = sorted(p.name for p in controls.iterdir())
        capsys.readouterr()

        codes = [main(["--config", str(config_path), cmd]) for cmd in ("status", "suggest")]

        outputs = _json_objects(capsys.readouterr().out)
        assert codes == [1, 1]
        assert all("partial" in out["error"] for out in outputs)
        assert sorted(p.name for p in controls.iterdir()) == before
        assert server.fixture.paths() == []


def _hold_unknown_month(config_path: Path) -> None:
    (Path.home() / ".apprentice" / "logs").mkdir(parents=True)


def _suspend(config_path: Path) -> None:
    rollback = ["controls", "prepare-rollback", "--operator", "operator@example.invalid"]
    assert main(["--config", str(config_path), *rollback]) == 0


def _live_legacy(config_path: Path) -> None:
    _write_record(default_store_dir(), "selection-legacy", "in_progress", datetime.now(tz=UTC))


def _invalid_route(config_path: Path) -> None:
    profile = Path(load_config(config_path).provider.accounting_profile_path)
    profile.write_text("{}")


class TestStatusAdmission:
    @pytest.mark.parametrize(
        ("prepare", "control"),
        [
            (_hold_unknown_month, "budget.global"),
            (_suspend, "controls.continuity"),
            (_live_legacy, "controls.legacy"),
            (_invalid_route, "provider"),
        ],
        ids=["unknown-month", "suspended", "live-legacy", "invalid-route"],
    )
    def test_blocked_route_is_reported_not_admissible_and_generation_is_denied(
        self,
        config_path: Path,
        server: ResponsesServer,
        capsys: pytest.CaptureFixture[str],
        prepare: Any,
        control: str,
    ) -> None:
        prepare(config_path)
        capsys.readouterr()

        status_code = main(["--config", str(config_path), "status"])
        route = json.loads(capsys.readouterr().out)["route"]
        suggest_code = main(["--config", str(config_path), "suggest", "--limit", "1"])

        assert status_code == 0
        assert route["admissible"] is False
        assert control in [blocker["control"] for blocker in route["blocked_by"]]
        assert suggest_code == 1
        assert server.fixture.paths() == []

    def test_admissible_route_is_reported_and_generation_is_counted_then_sent(
        self, config_path: Path, server: ResponsesServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["--config", str(config_path), "status"])
        route = json.loads(capsys.readouterr().out)["route"]

        code = main(["--config", str(config_path), "suggest", "--limit", "1"])

        assert (route["admissible"], route["blocked_by"]) == (True, [])
        assert code == 0
        assert server.fixture.paths() == ["/v1/responses/input_tokens", "/v1/responses"]


class TestOperatorSignals:
    def test_sigterm_cancels_the_cycle_and_keeps_its_dispatched_bound_unknown(
        self, tmp_path: Path, config_path: Path, server: ResponsesServer
    ) -> None:
        server.fixture.gate = threading.Event()
        cli = subprocess.Popen(
            [sys.executable, "-m", "apprentice.cli", "--config", str(config_path), "suggest"],
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "HOME": str(Path.home())},
        )
        try:
            assert server.fixture.arrived.wait(timeout=60)
            cli.send_signal(signal.SIGTERM)
            out, _ = cli.communicate(timeout=60)
        finally:
            server.fixture.gate.set()
            if cli.poll() is None:
                cli.kill()

        conn = sqlite3.connect(default_store_dir() / "controls" / "accounting.sqlite3")
        outcome = conn.execute("SELECT outcome FROM cycles").fetchall()
        generation = conn.execute(
            "SELECT state FROM entries WHERE operation = 'generate'"
        ).fetchall()
        conn.close()
        assert cli.returncode == 128 + signal.SIGTERM
        assert _json_objects(out)[-1]["outcome"] == "cancelled"
        assert outcome == [("cancelled",)]
        assert generation == [("unknown",)]

    def test_sigterm_while_the_suggest_end_is_recorded_reports_the_completed_outcome(
        self, config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from apprentice.controls.authority import Cycle

        real_finish = Cycle.finish

        def stop_then_finish(self: Cycle, outcome: str, detail: str = "") -> None:
            os.kill(os.getpid(), signal.SIGTERM)
            real_finish(self, outcome, detail)

        monkeypatch.setattr(Cycle, "finish", stop_then_finish)

        code = main(["--config", str(config_path), "suggest", "--limit", "1"])

        result, reported = _json_objects(capsys.readouterr().out)
        conn = sqlite3.connect(default_store_dir() / "controls" / "accounting.sqlite3")
        cycles = conn.execute("SELECT cycle_id, state, outcome FROM cycles").fetchall()
        conn.close()
        assert code == 128 + signal.SIGTERM
        assert cycles == [(reported["cycle_id"], "terminal", "completed")]
        assert reported["outcome"] == "completed" and "run_id" not in reported
        assert result["accounting"]["cycle_id"] == reported["cycle_id"]

    def test_sigterm_before_any_cycle_of_the_command_ended_claims_no_outcome(
        self, config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        (earlier,) = _json_objects(capsys.readouterr().out)
        real_status = Authority.status

        def stop_then_status(self: Authority) -> dict[str, Any]:
            os.kill(os.getpid(), signal.SIGTERM)
            return real_status(self)

        monkeypatch.setattr(Authority, "status", stop_then_status)

        code = main(["--config", str(config_path), "status"])

        (reported,) = _json_objects(capsys.readouterr().out)
        conn = sqlite3.connect(default_store_dir() / "controls" / "accounting.sqlite3")
        cycles = conn.execute("SELECT cycle_id, outcome FROM cycles").fetchall()
        conn.close()
        assert code == 128 + signal.SIGTERM
        assert cycles == [(earlier["accounting"]["cycle_id"], "completed")]
        assert not {"outcome", "cycle_id", "run_id"} & reported.keys()


class TestImplicitDotenv:
    @pytest.mark.parametrize("mode", ["absent", "DEV"])
    def test_importing_the_product_never_loads_a_dotenv_file(self, mode: str) -> None:
        worker = Path(__file__).parent / "dotenv_worker.py"

        done = subprocess.run(
            [sys.executable, str(worker), mode],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "HOME": str(Path.home())},
        )

        assert json.loads(done.stdout) == {"load_dotenv_calls": 0, "litellm_imported": True}


def _integration_script() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "integration_test", Path(__file__).parent.parent / "scripts" / "integration_test.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestUsageReports:
    def test_metrics_counts_record_less_cycles_and_recovers_dead_owners_first(
        self, config_path: Path, server: ResponsesServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        server.fixture.arrived.clear()
        server.fixture.gate = threading.Event()
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).parent / "control_worker.py"),
                str(default_store_dir()),
                str(config_path),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert worker.stdout is not None
            assert worker.stdout.readline().strip() == "ADMITTED"
            assert server.fixture.arrived.wait(timeout=30)
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=30)
        finally:
            server.fixture.gate.set()
            if worker.poll() is None:
                worker.kill()
        capsys.readouterr()

        assert main(["--config", str(config_path), "metrics"]) == 0

        report = json.loads(capsys.readouterr().out)
        conn = sqlite3.connect(default_store_dir() / "controls" / "accounting.sqlite3")
        settled, unknown = conn.execute(
            "SELECT SUM(CASE WHEN state = 'settled' THEN charged_tokens END), "
            "SUM(CASE WHEN state = 'unknown' THEN bound_tokens END) FROM entries"
        ).fetchone()
        conn.close()
        assert report["total_runs"] == 0
        assert report["cycles_by_kind"] == {"suggest": 2}
        assert report["usage"]["known_zero_hosted"]["tokens"] == settled > 0
        assert report["usage"]["unknown_held"]["tokens"] == unknown > 0
        assert report["active_reservations"]["entries"] == 0

    def test_usage_of_given_runs_counts_only_their_cycles_and_entries(
        self, tmp_path: Path, config_path: Path
    ) -> None:
        store = SessionStore(store_dir=tmp_path / "sessions")
        authority = Authority.open(store.store_dir, _EMPTY)
        run_id = SessionStore.new_run_id("selection", 2)
        with authority.begin_cycle("build", _policy(config_path), run_id=run_id) as cycle:
            build_cycle = cycle.cycle_id
        _call(config_path, authority)

        scoped = authority.usage({run_id})
        everything = authority.usage()
        authority.close()

        assert [(c["cycle_id"], c["kind"]) for c in scoped.cycles] == [(build_cycle, "build")]
        assert scoped.entries == []
        assert sorted(c["kind"] for c in everything.cycles) == ["build", "suggest"]
        assert {e["kind"] for e in everything.entries} == {"suggest"}

    def test_damaged_ledger_is_never_reported_as_zero_usage(
        self,
        config_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        _execute("DROP TABLE quarantine")(default_store_dir() / "controls" / "accounting.sqlite3")
        monkeypatch.setattr(
            sys, "argv", ["integration_test.py", "--config", str(config_path), "--report-only"]
        )
        capsys.readouterr()

        codes = [main(["--config", str(config_path), "metrics"]), _integration_script().main()]

        captured = capsys.readouterr()
        assert codes == [1, 1]
        assert "control authority unavailable" in _json_objects(captured.out)[-1]["error"]
        assert "control authority unavailable" in captured.err
        assert "known_zero_hosted" not in captured.out


def _overrun(usage: dict[str, Any]) -> dict[str, Any]:
    usage["output_tokens"] += 10_000
    usage["total_tokens"] += 10_000
    return usage


class TestCyclesByKind:
    def test_metrics_counts_every_admitted_cycle_including_ones_without_entries(
        self, config_path: Path, server: ResponsesServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server.fixture.usage_patch = _overrun
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 1
        server.fixture.usage_patch = None
        # The profile is now quarantined: both later cycles are admitted, then
        # refused before any call, so they write no ledger entry.
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 1
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 1
        conn = sqlite3.connect(default_store_dir() / "controls" / "accounting.sqlite3")
        kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM cycles GROUP BY kind").fetchall())
        entryless = conn.execute(
            "SELECT COUNT(*) FROM cycles c WHERE NOT EXISTS "
            "(SELECT 1 FROM entries e WHERE e.cycle_id = c.cycle_id)"
        ).fetchone()[0]
        charged = conn.execute(
            "SELECT SUM(charged_tokens) FROM entries WHERE state = 'settled'"
        ).fetchone()[0]
        conn.close()
        capsys.readouterr()

        assert main(["--config", str(config_path), "metrics"]) == 0

        report = json.loads(capsys.readouterr().out)
        assert entryless == 2
        assert report["cycles_by_kind"] == kinds == {"suggest": 3}
        assert report["usage"]["known_zero_hosted"]["tokens"] == charged


def _guard_state(controls: Path) -> dict[str, bytes | None]:
    return {
        name: (controls / name).read_bytes() if (controls / name).exists() else None
        for name in ("authority.id", "accounting.sqlite3")
    }


class TestStaleEmptyFootprint:
    @pytest.mark.parametrize("appeared", ["marker-only", "ledger-only", "complete"])
    def test_authority_appearing_after_an_empty_capture_is_left_exactly_as_found(
        self,
        config_path: Path,
        server: ResponsesServer,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        appeared: str,
    ) -> None:
        import apprentice.core.cycles as cycles

        real_capture = cycles.capture_installation_footprint
        controls = default_store_dir() / "controls"
        found: dict[str, dict[str, bytes | None]] = {}

        def capture_then_another_writer(config: Any, store_dir: Path | None) -> Footprint:
            footprint = real_capture(config, store_dir)
            assert footprint.empty
            # Another apprentice process creates its authority between this
            # capture and the bootstrap that acts on it.
            writer = Authority.open(SessionStore().store_dir, footprint)
            with writer.begin_cycle("suggest", _policy(config_path)):
                pass
            writer.close()
            if appeared == "marker-only":
                (controls / "accounting.sqlite3").unlink()
            elif appeared == "ledger-only":
                (controls / "authority.id").unlink()
            found["before"] = _guard_state(controls)
            return footprint

        monkeypatch.setattr(cycles, "capture_installation_footprint", capture_then_another_writer)
        capsys.readouterr()

        code = main(["--config", str(config_path), "suggest", "--limit", "1"])

        after = _guard_state(controls)
        if appeared == "complete":
            conn = sqlite3.connect(controls / "accounting.sqlite3")
            cycles_seen = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
            conn.close()
            assert code == 0
            assert after["authority.id"] == found["before"]["authority.id"]
            assert cycles_seen == 2
        else:
            assert code == 1
            assert "partial" in _json_objects(capsys.readouterr().out)[-1]["error"]
            assert after == found["before"]
            assert server.fixture.paths() == []


def _census(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under `root` with its mode and, where readable, its bytes."""
    seen: dict[str, tuple[int, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        data: bytes | None = None
        if path.is_file() and os.access(path, os.R_OK):
            data = path.read_bytes()
        seen[str(path.relative_to(root))] = (info.st_mode, data)
    return seen


def _non_utf8_marker(home: Path) -> None:
    (home / ".apprentice" / "sessions" / "controls" / "authority.id").write_bytes(b"\xff\xfe\n")


def _unreadable_ledger(home: Path) -> None:
    (home / ".apprentice" / "sessions" / "controls" / "accounting.sqlite3").chmod(0)


def _controls_is_a_file(home: Path) -> None:
    controls = home / ".apprentice" / "sessions" / "controls"
    controls.rename(controls.with_name("controls-moved-aside"))
    controls.write_text("not a directory\n")


def _controls_is_a_symlink(home: Path) -> None:
    controls = home / ".apprentice" / "sessions" / "controls"
    moved = controls.rename(controls.with_name("controls-moved-aside"))
    controls.symlink_to(moved, target_is_directory=True)


def _store_is_a_file(home: Path) -> None:
    store = home / ".apprentice" / "sessions"
    store.rename(store.with_name("sessions-moved-aside"))
    store.write_text("not a directory\n")


def _fresh_store_not_writable(home: Path) -> None:
    shutil_rmtree(home / ".apprentice")
    store = home / ".apprentice" / "sessions"
    store.mkdir(parents=True)
    store.chmod(0o500)


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)


class TestUnusableAuthorityFiles:
    @pytest.mark.parametrize(
        "damage",
        [
            _non_utf8_marker,
            _unreadable_ledger,
            _controls_is_a_file,
            _controls_is_a_symlink,
            _store_is_a_file,
            _fresh_store_not_writable,
        ],
        ids=[
            "marker-not-utf8",
            "ledger-mode-000",
            "controls-is-a-file",
            "controls-is-a-symlink",
            "store-is-a-file",
            "fresh-store-not-writable",
        ],
    )
    def test_unusable_files_are_reported_by_every_consumer_without_requests_or_changes(
        self,
        config_path: Path,
        server: ResponsesServer,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        damage: Any,
    ) -> None:
        home = Path.home()
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        sent = server.fixture.paths()
        damage(home)
        before = _census(home / ".apprentice")
        monkeypatch.setattr(
            sys, "argv", ["integration_test.py", "--config", str(config_path), "--report-only"]
        )
        capsys.readouterr()
        try:
            codes = [
                main(["--config", str(config_path), command])
                for command in ("status", "suggest", "metrics")
            ]
            codes.append(_integration_script().main())
            after = _census(home / ".apprentice")
        finally:
            for path in [home / ".apprentice", *(home / ".apprentice").rglob("*")]:
                if not path.is_symlink():
                    path.chmod(0o700)

        captured = capsys.readouterr()
        outputs = _json_objects(captured.out)
        assert codes == [1, 1, 1, 1]
        assert len(outputs) == 3
        assert all(
            out["error"].startswith(("control authority unavailable:", "run store unavailable:"))
            for out in outputs
        )
        assert "Traceback" not in captured.err
        assert captured.err.startswith(("control authority unavailable:", "run store unavailable:"))
        assert server.fixture.paths() == sent
        assert after == before

    @pytest.mark.parametrize("hidden", ["apprentice-home", "log-parent"])
    def test_unsearchable_parent_of_a_state_root_is_reported_by_every_consumer_unchanged(
        self,
        tmp_path: Path,
        server: ResponsesServer,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        hidden: str,
    ) -> None:
        home = Path.home()
        config = write_config(
            tmp_path / "logged.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            log_path=str(tmp_path / "log-parent" / "logs"),
        )
        assert main(["--config", str(config), "suggest", "--limit", "1"]) == 0
        sent = server.fixture.paths()
        parent, blocked = (
            (home / ".apprentice", home / ".apprentice" / "sessions")
            if hidden == "apprentice-home"
            else (tmp_path / "log-parent", tmp_path / "log-parent" / "logs")
        )
        roots = (home / ".apprentice", tmp_path / "log-parent")
        before = [_census(root) for root in roots]
        monkeypatch.setattr(
            sys, "argv", ["integration_test.py", "--config", str(config), "--report-only"]
        )
        capsys.readouterr()
        original = parent.stat().st_mode & 0o777
        parent.chmod(0o600)
        try:
            codes = [
                main(["--config", str(config), command])
                for command in ("config", "status", "suggest", "metrics")
            ]
            codes.append(_integration_script().main())
            mode = parent.lstat().st_mode & 0o777
        finally:
            parent.chmod(original)

        captured = capsys.readouterr()
        errors = [out["error"] for out in _json_objects(captured.out)]
        assert codes == [1, 1, 1, 1, 1]
        assert len(errors) == 4
        assert all(str(blocked) in error for error in errors)
        assert str(blocked) in captured.err
        assert "Traceback" not in captured.err
        assert mode == 0o600
        assert [_census(root) for root in roots] == before
        assert server.fixture.paths() == sent

    def test_run_record_behind_an_unsearchable_store_is_reported_by_retry_and_approve(
        self, config_path: Path, server: ResponsesServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        store = default_store_dir()
        run_id = SessionStore.new_run_id("selection", 2)
        _write_record(store, run_id, "failed", datetime.now(tz=UTC))
        sent = server.fixture.paths()
        before = _census(store.parent)
        capsys.readouterr()
        original = store.stat().st_mode & 0o777
        store.chmod(0o600)
        try:
            codes = [
                main(["--config", str(config_path), "retry", run_id]),
                main(["--config", str(config_path), "approve", run_id, "--approver", "tester"]),
            ]
            mode = store.lstat().st_mode & 0o777
        finally:
            store.chmod(original)

        captured = capsys.readouterr()
        retry, approve = _json_objects(captured.out)
        assert codes == [1, 1]
        assert all(str(store / f"{run_id}.json") in out["error"] for out in (retry, approve))
        assert "Traceback" not in captured.err
        assert mode == 0o600
        assert _census(store.parent) == before
        assert server.fixture.paths() == sent

    def test_run_record_without_read_permission_is_reported_by_every_reader_unchanged(
        self,
        config_path: Path,
        server: ResponsesServer,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        store = default_store_dir()
        run_id = SessionStore.new_run_id("selection", 2)
        _write_record(store, run_id, "failed", datetime.now(tz=UTC))
        record = store / f"{run_id}.json"
        sent, before, data = server.fixture.paths(), _census(store.parent), record.read_bytes()
        monkeypatch.setattr(
            sys, "argv", ["integration_test.py", "--config", str(config_path), "--report-only"]
        )
        capsys.readouterr()
        original = record.stat().st_mode & 0o777
        record.chmod(0o000)
        try:
            commands = [
                ["retry", run_id],
                ["approve", run_id, "--approver", "tester"],
                ["preview", "--run-id", run_id],
                ["submit", "selection", "--run-id", run_id],
                ["history"],
                ["metrics"],
                ["status"],
            ]
            codes = [main(["--config", str(config_path), *command]) for command in commands]
            codes.append(_integration_script().main())
            mode = record.lstat().st_mode & 0o777
        finally:
            record.chmod(original)

        captured = capsys.readouterr()
        errors = [out["error"] for out in _json_objects(captured.out)]
        assert codes == [1] * 8
        assert len(errors) == 7
        assert all(str(record) in error for error in errors)
        assert str(record) in captured.err
        assert "Traceback" not in captured.err
        after = _census(store.parent)
        assert mode == 0
        assert record.read_bytes() == data
        # Only approve's own record lock (created before the record is read) is new.
        assert set(after) - set(before) <= {"sessions/runs", f"sessions/runs/{run_id}.lock"}
        assert {name: after[name] for name in before} == before
        assert server.fixture.paths() == sent

    @pytest.mark.parametrize("state", ["readable", "read-only", "missing", "corrupt"])
    def test_run_record_that_can_be_read_or_is_absent_keeps_its_meaning(
        self, config_path: Path, capsys: pytest.CaptureFixture[str], state: str
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        store = default_store_dir()
        run_id = SessionStore.new_run_id("selection", 2)
        record = store / f"{run_id}.json"
        if state != "missing":
            _write_record(store, run_id, "failed", datetime.now(tz=UTC))
        if state == "read-only":
            record.chmod(0o400)
        if state == "corrupt":
            record.write_text("{not json\n")
        capsys.readouterr()

        codes = [
            main(["--config", str(config_path), "history"]),
            main(["--config", str(config_path), "approve", run_id, "--approver", "tester"]),
        ]

        history, approve = _json_objects(capsys.readouterr().out)
        if state in ("readable", "read-only"):
            assert codes == [0, 1]
            assert [(r["run_id"], r["status"]) for r in history["runs"]] == [(run_id, "failed")]
            assert run_id in approve["error"] and str(record) not in approve["error"]
        elif state == "missing":
            assert codes == [0, 1]
            assert history["runs"] == []
            assert run_id in approve["error"] and str(record) not in approve["error"]
        else:
            assert codes == [1, 1]
            assert all(str(record) in out["error"] for out in (history, approve))
        assert not any(
            out.get("error", "").startswith(("run store", "control authority"))
            for out in (history, approve)
        )

    def test_authority_guard_that_cannot_be_locked_is_reported_without_requests(
        self,
        config_path: Path,
        server: ResponsesServer,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import errno

        import apprentice.controls.authority as authority_module

        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        sent = server.fixture.paths()
        real_flock = fcntl.flock

        def flock(fd: int, operation: int) -> None:
            if operation == fcntl.LOCK_EX:  # the blocking bootstrap guard only
                raise OSError(errno.ENOLCK, "No locks available")
            real_flock(fd, operation)

        monkeypatch.setattr(authority_module.fcntl, "flock", flock)
        capsys.readouterr()

        codes = [main(["--config", str(config_path), c]) for c in ("status", "suggest")]

        captured = capsys.readouterr()
        assert codes == [1, 1]
        assert all("cannot be locked" in o["error"] for o in _json_objects(captured.out))
        assert "Traceback" not in captured.err
        assert server.fixture.paths() == sent


class TestUnlistableStore:
    @pytest.mark.parametrize(
        "mode", [0o700, 0o300, 0o100], ids=["listable", "write-and-search-only", "search-only"]
    )
    def test_a_store_that_cannot_be_listed_is_never_read_as_holding_no_records(
        self,
        config_path: Path,
        server: ResponsesServer,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mode: int,
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        store = default_store_dir()
        legacy = "selection-20260115T000000Z"
        _write_record(store, legacy, "in_progress", datetime(2026, 1, 15, tzinfo=UTC))
        database = store / "controls" / "accounting.sqlite3"
        sent, before, ledger = server.fixture.paths(), _census(store.parent), database.read_bytes()
        monkeypatch.setattr(
            sys, "argv", ["integration_test.py", "--config", str(config_path), "--report-only"]
        )
        capsys.readouterr()
        original = store.stat().st_mode & 0o777
        store.chmod(mode)
        try:
            codes = [
                main(["--config", str(config_path), *command])
                for command in (["status"], ["suggest", "--limit", "1"], ["history"], ["metrics"])
            ]
            captured = capsys.readouterr()
            codes.append(_integration_script().main())
            script = capsys.readouterr()
        finally:
            store.chmod(original)

        status, suggest, history, metrics = _json_objects(captured.out)
        assert server.fixture.paths() == sent
        assert "Traceback" not in captured.err + script.err
        if mode == 0o700:
            assert codes == [0, 1, 0, 0, 0]
            assert status["route"]["admissible"] is False
            assert suggest["control"] == "controls.legacy"
            assert [run["run_id"] for run in history["runs"]] == [legacy]
            assert metrics["in_progress_runs"] == 1
        else:
            assert codes == [1] * 5
            assert all(str(store) in out["error"] for out in (status, suggest, history, metrics))
            assert str(store) in script.err
            assert database.read_bytes() == ledger
            assert _census(store.parent) == before


class TestRunsRootThatIsNotOwned:
    def test_a_symlinked_runs_root_is_refused_typed_and_nothing_is_written_through_it(
        self,
        tmp_path: Path,
        config_path: Path,
        server: ResponsesServer,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert main(["--config", str(config_path), "status"]) == 0
        store = default_store_dir()
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "sentinel").write_text("untouched\n")
        (store / "runs").symlink_to(target, target_is_directory=True)
        monkeypatch.setattr(
            sys,
            "argv",
            ["integration_test.py", "--config", str(config_path), "--tier", "2", "--limit", "1"],
        )
        capsys.readouterr()

        codes = [main(["--config", str(config_path), "build", "selection"])]
        captured = capsys.readouterr()
        codes.append(_integration_script().main())
        script = capsys.readouterr()

        conn = sqlite3.connect(store / "controls" / "accounting.sqlite3")
        cycles = conn.execute("SELECT kind, state, outcome FROM cycles").fetchall()
        conn.close()
        out = json.loads(captured.out[captured.out.rfind("\n{") + 1 :])
        assert codes == [1, 1]
        assert str(store / "runs") in out["error"]
        assert str(store / "runs") in script.err
        assert "Traceback" not in captured.err + script.err
        assert sorted(cycles) == [
            ("build", "terminal", "failed"),
            ("library", "terminal", "failed"),
        ]
        assert server.fixture.paths() == []
        assert sorted(p.name for p in target.iterdir()) == ["sentinel"]
        assert list(store.glob("*.json")) == []


class TestStatusPolicyConflict:
    def test_live_cycle_under_another_configuration_blocks_the_route_and_its_generation(
        self,
        tmp_path: Path,
        config_path: Path,
        server: ResponsesServer,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Two concurrent items, so the live cycle itself blocks nothing here.
        config_path = write_config(
            tmp_path / "two-items.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            max_concurrent_items=2,
        )
        other = write_config(
            tmp_path / "other.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            monthly_token_ceiling=1_999_999,
            max_concurrent_items=2,
        )
        main(["--config", str(config_path), "status"])
        authority = Authority.open(default_store_dir(), _EMPTY)
        live = authority.begin_cycle("build", _policy(config_path))
        capsys.readouterr()

        same = main(["--config", str(config_path), "status"])
        same_route = json.loads(capsys.readouterr().out)["route"]
        main(["--config", str(other), "status"])
        other_route = json.loads(capsys.readouterr().out)["route"]
        denied = main(["--config", str(other), "suggest", "--limit", "1"])
        denial = _json_objects(capsys.readouterr().out)[-1]
        sent_while_live = server.fixture.paths()
        live.finish("completed")
        authority.close()
        main(["--config", str(other), "status"])
        after_route = json.loads(capsys.readouterr().out)["route"]
        admitted = main(["--config", str(other), "suggest", "--limit", "1"])

        assert (same, same_route["admissible"], same_route["blocked_by"]) == (0, True, [])
        assert other_route["admissible"] is False
        assert [b["control"] for b in other_route["blocked_by"]] == ["controls.policy"]
        assert (denied, denial["control"], sent_while_live) == (1, "controls.policy", [])
        assert (after_route["admissible"], after_route["blocked_by"]) == (True, [])
        assert admitted == 0


class TestDelayBounds:
    @pytest.mark.parametrize(
        "limits",
        [
            {"cooldown_hours": "1e8"},
            {"half_open_probe_after_minutes": "5e9"},
            {"cooldown_hours": "1e999999"},
            {"max_cost_per_cycle_usd": "1e999999"},
            {"monthly_cost_ceiling_usd": "1e999999"},
        ],
        ids=[
            "cooldown-hours",
            "probe-minutes",
            "cooldown-hours-decimal-overflow",
            "cycle-usd-decimal-overflow",
            "monthly-usd-decimal-overflow",
        ],
    )
    def test_loaded_value_beyond_what_can_be_represented_is_refused_before_anything(
        self,
        tmp_path: Path,
        server: ResponsesServer,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        limits: dict[str, str],
    ) -> None:
        huge = write_config(
            tmp_path / "huge.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            **limits,
        )
        monkeypatch.setattr(
            sys, "argv", ["integration_test.py", "--config", str(huge), "--report-only"]
        )

        codes = [
            main(["--config", str(huge), *command])
            for command in (["config"], ["status"], ["suggest", "--limit", "1"])
        ]
        codes.append(_integration_script().main())

        (key,) = limits
        captured = capsys.readouterr()
        errors = [out["error"] for out in _json_objects(captured.out)]
        assert codes == [1, 1, 1, 1]
        assert len(errors) == 3
        assert all(error.startswith("invalid configuration:") and key in error for error in errors)
        assert key in captured.err
        assert "Traceback" not in captured.err
        assert server.fixture.paths() == []
        assert not (Path.home() / ".apprentice").exists()

    def test_large_representable_delays_are_accepted(
        self, tmp_path: Path, server: ResponsesServer
    ) -> None:
        large = write_config(
            tmp_path / "large.toml",
            profile=local_profile(tmp_path / "profile.json"),
            base_url=server.base_url,
            cooldown_hours="1e6",
            half_open_probe_after_minutes="5e7",
            max_cost_per_cycle_usd="0.000000001",
        )

        assert main(["--config", str(large), "suggest", "--limit", "1"]) == 0

    @pytest.mark.parametrize(
        ("key", "value"),
        [("cooldown_hours", "1e8"), ("half_open_probe_after_minutes", "5e9")],
    )
    def test_stored_policy_with_an_unrepresentable_delay_fails_closed_and_is_kept(
        self,
        config_path: Path,
        server: ResponsesServer,
        capsys: pytest.CaptureFixture[str],
        key: str,
        value: str,
    ) -> None:
        assert main(["--config", str(config_path), "suggest", "--limit", "1"]) == 0
        database = default_store_dir() / "controls" / "accounting.sqlite3"
        conn = sqlite3.connect(database)
        (stored,) = conn.execute("SELECT value FROM meta WHERE key = 'policy'").fetchone()
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'policy'",
            (json.dumps({**json.loads(stored), key: value}),),
        )
        conn.commit()
        conn.close()
        before, sent = database.read_bytes(), server.fixture.paths()
        capsys.readouterr()

        codes = [main(["--config", str(config_path), c]) for c in ("status", "suggest", "metrics")]

        captured = capsys.readouterr()
        assert codes == [1, 1, 1]
        assert all(
            "control authority unavailable" in out["error"] for out in _json_objects(captured.out)
        )
        assert "Traceback" not in captured.err
        assert server.fixture.paths() == sent
        assert database.read_bytes() == before


class TestRecordsWrittenDuringARescan:
    def _status_while(self, config_path: Path, tmp_path: Path, write: Any) -> tuple[int, str, str]:
        pause = tmp_path / "pause"
        pause.mkdir()
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).parent / "scan_worker.py"),
                str(pause),
                "--config",
                str(config_path),
                "status",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = datetime.now(tz=UTC) + timedelta(seconds=60)
            while not (pause / "reached").exists():
                assert worker.poll() is None and datetime.now(tz=UTC) < deadline
                threading.Event().wait(0.01)
            clock = datetime.fromisoformat((pause / "reached").read_text())
            write()
            (pause / "go").write_text("")
            out, err = worker.communicate(timeout=60)
        finally:
            if worker.poll() is None:
                worker.kill()
        assert clock < datetime.now(tz=UTC)
        return worker.returncode, out, err

    @pytest.mark.parametrize("change", ["created", "completed", "failed"])
    def test_a_record_written_after_the_ledger_clock_is_not_future_and_status_reports(
        self, tmp_path: Path, config_path: Path, server: ResponsesServer, change: str
    ) -> None:
        assert main(["--config", str(config_path), "status"]) == 0
        store = SessionStore()
        earlier = store.create_run("selection", 2) if change != "created" else None

        def write() -> None:
            if earlier is None:
                store.create_run("selection", 2)
            elif change == "completed":
                store.complete_run(earlier, {"generated_code": "x = 1\n"}, {}, 1.0)
            else:
                store.fail_run(earlier, {}, {}, 1.0, "failed elsewhere")

        code, out, err = self._status_while(config_path, tmp_path, write)

        assert code == 0, err
        assert json.loads(out)["route"]["structurally_valid"] is True

    @pytest.mark.parametrize("started", ["while-the-records-are-read", "tomorrow"])
    def test_a_submission_started_after_the_ledger_clock_is_counted_and_a_future_one_refused(
        self, tmp_path: Path, config_path: Path, server: ResponsesServer, started: str
    ) -> None:
        assert main(["--config", str(config_path), "status"]) == 0
        store = SessionStore()
        record = store.create_run("selection", 2)
        stamped: list[str] = []

        def write() -> None:
            moment = datetime.now(tz=UTC)
            if started == "tomorrow":
                moment += timedelta(days=1)
            stamped.append(moment.isoformat())
            current = store.load(record.run_id)
            current.submission = {
                "status": "complete",
                "started_at": moment.isoformat(),
                "repositories": [
                    {"repository": r, "pushed": True, "pr_url": f"https://github.com/{r}/pull/1"}
                    for r in ("no-magic-ai/no-magic", "no-magic-ai/no-magic-viz")
                ],
            }
            store.save(current)

        code, _out, err = self._status_while(config_path, tmp_path, write)

        conn = sqlite3.connect(default_store_dir() / "controls" / "accounting.sqlite3")
        slots = conn.execute("SELECT run_id, at FROM pr_slots WHERE state = 'legacy'").fetchall()
        conn.close()
        assert "Traceback" not in err
        if started == "tomorrow":
            assert (code, slots) == (1, [])
        else:
            assert code == 0, err
            assert slots == [(record.run_id, stamped[0])] * 2

    def test_a_record_stamped_in_the_future_still_fails_closed(
        self, tmp_path: Path, config_path: Path, server: ResponsesServer
    ) -> None:
        assert main(["--config", str(config_path), "status"]) == 0
        future = datetime.now(tz=UTC) + timedelta(days=1)

        code, out, err = self._status_while(
            config_path,
            tmp_path,
            lambda: _write_record(default_store_dir(), "selection-future", "failed", future),
        )

        error = json.loads(out)["error"]
        assert (code, "Traceback" in err) == (1, False)
        assert error.startswith("control authority unavailable") and "in the future" in error
