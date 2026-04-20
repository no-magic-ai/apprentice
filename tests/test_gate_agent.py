"""Tests for the GateAgent ADK wrapper and materialize_artifacts helper."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from apprentice.core.budget import BudgetTracker
from apprentice.core.gate_agent import GateAgent, materialize_artifacts
from apprentice.models.work_item import GateResult, GateVerdict, WorkItem

if TYPE_CHECKING:
    from apprentice.models.artifact import ArtifactBundle


class _StubGate:
    name = "stub"
    max_retries = 0
    blocking = True

    def __init__(self, verdict: GateVerdict) -> None:
        self._verdict = verdict
        self.seen: list[str] = []

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        self.seen.append(work_item.algorithm_name)
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


class TestMaterializeArtifacts:
    def test_writes_every_provided_output(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "apprentice.core.gate_agent.tempfile.gettempdir", lambda: str(tmp_path)
        )
        state = {
            "algorithm_name": "selection",
            "generated_code": "print('impl')\n",
            "instrumented_code": "print('instr')\n",
            "manim_scene_code": "print('scene')\n",
            "anki_deck_content": "front,back,tags,type\n",
        }
        bundle = materialize_artifacts(state)
        assert Path(bundle.implementation_path).read_text() == "print('impl')\n"
        assert Path(bundle.instrumented_path).read_text() == "print('instr')\n"
        assert Path(bundle.manim_scene_path).read_text() == "print('scene')\n"
        assert Path(bundle.anki_deck_path).exists()

    def test_omits_absent_outputs(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "apprentice.core.gate_agent.tempfile.gettempdir", lambda: str(tmp_path)
        )
        bundle = materialize_artifacts({"algorithm_name": "selection"})
        assert bundle.implementation_path == ""
        assert bundle.instrumented_path == ""


async def _collect(agen: Any) -> list[Any]:
    return [event async for event in agen]


class TestGateAgent:
    def test_pass_records_verdict_and_continues(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "apprentice.core.gate_agent.tempfile.gettempdir", lambda: str(tmp_path)
        )
        tracker = BudgetTracker(total_tokens=1000, total_usd=1.0)
        gate = _StubGate(GateVerdict.PASS)
        agent = GateAgent(gate, after_stage="implementation", tracker=tracker)
        ctx = _StubCtx(
            state={"algorithm_name": "selection", "generated_code": "print('ok')\n"}
        )

        events = anyio.run(_collect, agent._run_async_impl(ctx))

        assert len(events) == 1
        assert ctx.end_invocation is False
        assert ctx.session.state["gate_verdicts"][-1]["verdict"] == GateVerdict.PASS.value
        assert tracker.gate_verdicts[-1]["gate_name"] == "stub"
        assert tracker.gate_verdicts[-1]["after_stage"] == "implementation"

    def test_blocking_fail_ends_invocation(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "apprentice.core.gate_agent.tempfile.gettempdir", lambda: str(tmp_path)
        )
        tracker = BudgetTracker(total_tokens=1000, total_usd=1.0)
        agent = GateAgent(
            _StubGate(GateVerdict.FAIL), after_stage="implementation", tracker=tracker
        )
        ctx = _StubCtx(
            state={"algorithm_name": "selection", "generated_code": "print('ok')\n"}
        )

        events = anyio.run(_collect, agent._run_async_impl(ctx))

        assert len(events) == 1
        assert ctx.end_invocation is True
        assert ctx.session.state["gate_verdicts"][-1]["verdict"] == GateVerdict.FAIL.value
