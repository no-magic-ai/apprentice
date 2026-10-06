"""Tests for the GateAgent ADK wrapper."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from apprentice.core.artifacts import ArtifactError, RunScope
from apprentice.core.budget import BudgetTracker
from apprentice.core.gate_agent import GateAgent
from apprentice.models.work_item import GateResult, GateVerdict, WorkItem

if TYPE_CHECKING:
    from apprentice.core.session_store import SessionStore
    from apprentice.models.artifact import ArtifactBundle


class _StubGate:
    name = "stub"
    max_retries = 0
    blocking = True

    def __init__(self, verdict: GateVerdict) -> None:
        self._verdict = verdict
        self.seen: list[tuple[WorkItem, ArtifactBundle]] = []

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        self.seen.append((work_item, artifacts))
        return GateResult(
            gate_name=self.name,
            verdict=self._verdict,
            diagnostics={"impl": artifacts.implementation_path},
        )


class _StubSession:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.id = "test-session"


class _StubCtx:
    def __init__(self, state: dict[str, Any]) -> None:
        self.session = _StubSession(state)
        self.invocation_id = "inv-1"
        self.branch = None
        self.end_invocation = False


async def _collect(agen: Any) -> list[Any]:
    return [event async for event in agen]


class TestGateAgent:
    def test_pass_records_verdict_and_continues(self, scope: RunScope) -> None:
        tracker = BudgetTracker(total_tokens=1000, total_usd=1.0)
        gate = _StubGate(GateVerdict.PASS)
        agent = GateAgent.after(gate, "implementation", scope, tracker=tracker)
        ctx = _StubCtx(state={"algorithm_name": "selection", "generated_code": "print('ok')\n"})

        events = anyio.run(_collect, agent._run_async_impl(ctx))

        assert len(events) == 1
        assert ctx.end_invocation is False
        assert ctx.session.state["gate_verdicts"][-1]["verdict"] == GateVerdict.PASS.value
        assert tracker.gate_verdicts[-1]["gate_name"] == "stub"
        assert tracker.gate_verdicts[-1]["after_stage"] == "implementation"

    def test_blocking_fail_ends_invocation(self, scope: RunScope) -> None:
        tracker = BudgetTracker(total_tokens=1000, total_usd=1.0)
        agent = GateAgent.after(
            _StubGate(GateVerdict.FAIL), "implementation", scope, tracker=tracker
        )
        ctx = _StubCtx(state={"algorithm_name": "selection", "generated_code": "print('ok')\n"})

        events = anyio.run(_collect, agent._run_async_impl(ctx))

        assert len(events) == 1
        assert ctx.end_invocation is True
        assert ctx.session.state["gate_verdicts"][-1]["verdict"] == GateVerdict.FAIL.value

    def test_gate_sees_run_identity_and_files_in_its_work_root(self, scope: RunScope) -> None:
        gate = _StubGate(GateVerdict.PASS)
        agent = GateAgent.after(gate, "artifact_generation", scope)
        ctx = _StubCtx(
            state={
                "algorithm_name": "state-claims-another-name",
                "algorithm_tier": 4,
                "generated_code": "print('impl')\n",
                "manim_scene_code": "print('scene')\n",
            }
        )

        anyio.run(_collect, agent._run_async_impl(ctx))

        work_item, bundle = gate.seen[-1]
        assert (work_item.id, work_item.algorithm_name, work_item.tier) == (
            scope.run_id,
            "selection",
            2,
        )
        assert Path(bundle.implementation_path) == scope.work_root / "implementation.py"
        assert Path(bundle.manim_scene_path).read_text() == "print('scene')\n"
        assert bundle.instrumented_path == ""

    def test_same_algorithm_runs_do_not_share_gate_files(self, store: SessionStore) -> None:
        first = store.run_scope(store.create_run("selection", 2))
        second = store.run_scope(store.create_run("selection", 2))
        first_gate = _StubGate(GateVerdict.PASS)
        second_gate = _StubGate(GateVerdict.PASS)

        anyio.run(
            _collect,
            GateAgent.after(first_gate, "implementation", first)._run_async_impl(
                _StubCtx(state={"generated_code": "first = 1\n"})
            ),
        )
        anyio.run(
            _collect,
            GateAgent.after(second_gate, "implementation", second)._run_async_impl(
                _StubCtx(state={"generated_code": "second = 2\n"})
            ),
        )

        first_path = Path(first_gate.seen[-1][1].implementation_path)
        assert first_path.read_text() == "first = 1\n"
        assert first_path != Path(second_gate.seen[-1][1].implementation_path)

    def test_symlinked_work_root_is_refused_before_the_gate_runs(
        self, scope: RunScope, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        link = tmp_path / "link"
        link.symlink_to(outside)
        gate = _StubGate(GateVerdict.PASS)
        agent = GateAgent.after(
            gate,
            "implementation",
            RunScope(scope.run_id, scope.algorithm, scope.tier, link),
        )

        with pytest.raises(ArtifactError, match="symlink"):
            anyio.run(
                _collect,
                agent._run_async_impl(_StubCtx(state={"generated_code": "x = 1\n"})),
            )
        assert gate.seen == []
        assert list(outside.iterdir()) == []
