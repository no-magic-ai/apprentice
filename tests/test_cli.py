"""Tests for CLI entry point."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from apprentice.core.config import load_config
from apprentice.core.session_store import RunRecord, SessionStore
from apprentice.models.work_item import BlockingGateError
from tests.conftest import OfflineFixtureLlm, fixture_outputs

if TYPE_CHECKING:
    from collections.abc import Callable

    from apprentice.core.artifacts import RunScope


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
    def __init__(self, algorithm: str, run_id: str | None, tier: int | None = None) -> None:
        self.algorithm = algorithm
        self.tier = tier
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


def _seal_from_build(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str], tier: int = 2
) -> RunRecord:
    _use_model(monkeypatch, outputs)
    assert _cmd_build(load_config(None), _BuildArgs(tier=tier)) == 0
    record = _only_record(store_dir)
    assert _cmd_approve(_ApproveArgs(record.run_id)) == 0
    return SessionStore(store_dir=store_dir).load(record.run_id)


def _bundle_bytes(store_dir: Path, run_id: str) -> dict[str, tuple[bytes, int]]:
    bundle = SessionStore(store_dir=store_dir).bundle_dir(run_id)
    return {p.name: (p.read_bytes(), p.stat().st_mode) for p in sorted(bundle.iterdir())}


class TestRootSubmitRegeneration:
    @pytest.mark.parametrize(
        ("regenerated", "diffs"),
        [
            (
                dict(fixture_outputs("A"), drafter=fixture_outputs("B")["drafter"]),
                {"implementation"},
            ),
            (fixture_outputs("B"), {"implementation", "instrumented", "manim_scene", "anki_deck"}),
            (fixture_outputs("A", omit=("assessment",)), {"anki_deck"}),
        ],
    )
    def test_mismatch_halts_before_the_publisher_and_leaves_approval_untouched(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        regenerated: dict[str, str],
        diffs: set[str],
    ) -> None:
        approved = _seal_from_build(store_dir, monkeypatch, fixture_outputs("A"))
        sealed = _bundle_bytes(store_dir, approved.run_id)
        record_bytes = (store_dir / f"{approved.run_id}.json").read_bytes()
        llm = _use_model(monkeypatch, regenerated)
        capsys.readouterr()

        assert _cmd_submit(load_config(None), _SubmitArgs("selection", approved.run_id)) == 1

        out = json.loads(_last_json(capsys))
        assert out["error"] == "blocking gate failed: review after review"
        assert set(out["gate"]["diagnostics"]["diffs"]) == diffs
        assert "packaging" not in llm.roles()
        assert _bundle_bytes(store_dir, approved.run_id) == sealed
        assert (store_dir / f"{approved.run_id}.json").read_bytes() == record_bytes

    def test_identical_regeneration_reaches_the_publisher_with_fresh_paths(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approved = _seal_from_build(store_dir, monkeypatch, fixture_outputs("A"))
        llm = _use_model(monkeypatch, fixture_outputs("A"))

        assert _cmd_submit(load_config(None), _SubmitArgs("selection", approved.run_id)) == 0

        (instruction,) = [text for role, text in llm.requests if role == "packaging"]
        store = SessionStore(store_dir=store_dir)
        (fresh,) = list((store_dir / "scratch").iterdir())
        assert f"Implementation: {fresh / 'implementation.py'}" in instruction
        assert f"Anki deck: {fresh / 'cards.csv'}" in instruction
        assert "no-magic/02-alignment/microselection.py" in instruction
        assert "no-magic-viz/scenes/scene_microselection.py" in instruction
        assert str(store.bundle_dir(approved.run_id)) not in instruction
        assert "{" not in instruction


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


class TestRootSubmitIdentity:
    """Root submit publishes only the identity of the verified sealed bundle it was approved for."""

    @pytest.mark.parametrize(
        ("algorithm", "tier", "refusal"),
        [
            ("insertion", None, "requested algorithm/tier does not match the approved run"),
            ("selection", 3, "requested algorithm/tier does not match the approved run"),
        ],
    )
    def test_conflicting_request_is_refused_before_any_work(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        algorithm: str,
        tier: int | None,
        refusal: str,
    ) -> None:
        approved = _seal_from_build(store_dir, monkeypatch, fixture_outputs("A"))
        self._assert_refused_before_any_work(
            store_dir, monkeypatch, capsys, _SubmitArgs(algorithm, approved.run_id, tier), refusal
        )

    @pytest.mark.parametrize(
        ("mutate", "refusal"),
        [
            (
                lambda r: r["approval"].update(run_id="0" * 32),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"].update(algorithm="insertion"),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"].update(tier=3),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"].update(tier="2"),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"].update(manifest_sha256="0" * 64),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"].pop("tier"),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"].update(tier=2.0),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"]["artifact_hashes"].update(implementation="0" * 64),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"]["artifact_hashes"].pop("anki_deck"),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"]["artifact_hashes"].update(extra="0" * 64),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"].pop("artifact_hashes"),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r["approval"].update(
                    artifact_hashes=sorted(r["approval"]["artifact_hashes"].items())
                ),
                "approval does not match the run's sealed bundle",
            ),
            (
                lambda r: r.update(approval=["approved"]),
                "stored approval of this run is not an object",
            ),
            (
                lambda r: r.update(approval="approved"),
                "stored approval of this run is not an object",
            ),
            (lambda r: r.update(approval=5), "stored approval of this run is not an object"),
            (lambda r: r.update(tier=2.0), "field 'tier' must be an integer"),
            (lambda r: r.update(tier=True), "field 'tier' must be an integer"),
            (lambda r: r.update(algorithm_name="insertion"), "bundle identity does not match"),
            (lambda r: r.update(tier=3), "bundle identity does not match"),
            (lambda r: r.update(run_id="0" * 32), "carries a different run ID"),
        ],
    )
    def test_damaged_stored_approval_or_record_is_refused_before_any_work(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mutate: Callable[[dict[str, Any]], None],
        refusal: str,
    ) -> None:
        approved = _seal_from_build(store_dir, monkeypatch, fixture_outputs("A"))
        _damage_record(store_dir, approved.run_id, mutate)
        self._assert_refused_before_any_work(
            store_dir, monkeypatch, capsys, _SubmitArgs("selection", approved.run_id), refusal
        )

    @staticmethod
    def _assert_refused_before_any_work(
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        args: _SubmitArgs,
        refusal: str,
    ) -> None:
        # The regeneration would match the approval, so only the refusal stops it.
        llm = OfflineFixtureLlm(model="offline-fixture", outputs=fixture_outputs("A"))
        resolved: list[object] = []

        def resolve(cfg: object, submit_args: object) -> OfflineFixtureLlm:
            resolved.append(submit_args)
            return llm

        monkeypatch.setattr("apprentice.cli._resolve_model", resolve)
        before = _tree(store_dir)
        capsys.readouterr()

        assert _cmd_submit(load_config(None), args) == 1

        assert refusal in json.loads(_last_json(capsys))["error"]
        assert resolved == []
        assert llm.requests == []
        assert _tree(store_dir) == before

    @pytest.mark.parametrize(
        ("built_tier", "asserted_tier", "tier_dir"),
        [(1, None, "01-foundations"), (3, None, "03-systems"), (3, 3, "03-systems")],
    )
    def test_publisher_receives_the_sealed_name_and_tier(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        built_tier: int,
        asserted_tier: int | None,
        tier_dir: str,
    ) -> None:
        approved = _seal_from_build(store_dir, monkeypatch, fixture_outputs("A"), tier=built_tier)
        sealed = _bundle_bytes(store_dir, approved.run_id)
        llm = _use_model(monkeypatch, fixture_outputs("A"))
        capsys.readouterr()

        args = _SubmitArgs("selection", approved.run_id, asserted_tier)
        assert _cmd_submit(load_config(None), args) == 0

        out = json.loads(_last_json(capsys))
        assert (out["run_id"], out["algorithm"], out["tier"]) == (
            approved.run_id,
            "selection",
            built_tier,
        )
        (instruction,) = [text for role, text in llm.requests if role == "packaging"]
        assert f"- Tier: {built_tier}\n" in instruction
        assert f"no-magic/{tier_dir}/microselection.py" in instruction
        assert "no-magic-viz/scenes/scene_microselection.py" in instruction
        assert "{" not in instruction
        tier_prompts = [text for text in llm.user_texts if "(tier " in text]
        assert tier_prompts
        assert all(f"selection algorithm (tier {built_tier})" in text for text in tier_prompts)
        assert _bundle_bytes(store_dir, approved.run_id) == sealed

    def test_role_absent_from_the_approval_gives_the_publisher_an_empty_path(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outputs = fixture_outputs("A", omit=("assessment",))
        approved = _seal_from_build(store_dir, monkeypatch, outputs)
        llm = _use_model(monkeypatch, outputs)

        assert _cmd_submit(load_config(None), _SubmitArgs("selection", approved.run_id)) == 0

        (instruction,) = [text for role, text in llm.requests if role == "packaging"]
        (fresh,) = list((store_dir / "scratch").iterdir())
        assert "- Anki deck: " in instruction.splitlines()
        assert f"Implementation: {fresh / 'implementation.py'}" in instruction
        assert not (fresh / "cards.csv").exists()


class TestSealedTierType:
    def test_float_tier_in_a_re_signed_bundle_bound_to_its_record_is_refused_before_any_work(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from apprentice.core.artifacts import MANIFEST_FILENAME, canonical_json, manifest_digest

        approved = _seal_from_build(store_dir, monkeypatch, fixture_outputs("A"))
        path = SessionStore(store_dir=store_dir).bundle_dir(approved.run_id) / MANIFEST_FILENAME
        path.chmod(0o644)
        manifest = json.loads(path.read_bytes())
        del manifest["manifest_sha256"]
        manifest["tier"] = 2.0
        digest = manifest_digest(manifest)
        path.write_bytes(canonical_json({**manifest, "manifest_sha256": digest}))

        def rebind(record: dict[str, Any]) -> None:
            record["manifest_sha256"] = digest
            record["approval"]["manifest_sha256"] = digest

        _damage_record(store_dir, approved.run_id, rebind)
        TestRootSubmitIdentity._assert_refused_before_any_work(
            store_dir,
            monkeypatch,
            capsys,
            _SubmitArgs("selection", approved.run_id),
            "unsupported tier 2.0",
        )


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
            lambda: _cmd_submit(load_config(None), _SubmitArgs("selection", None)),
        ],
        ids=["history", "metrics", "preview-default", "submit-latest"],
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
