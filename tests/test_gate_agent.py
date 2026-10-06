"""Tests for the GateAgent ADK wrapper."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from apprentice.core.artifacts import ArtifactError, RunScope
from apprentice.core.budget import BudgetTracker
from apprentice.core.gate_agent import GateAgent
from apprentice.models.work_item import BlockingGateError, GateResult, GateVerdict, WorkItem

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.agents.invocation_context import InvocationContext

    from apprentice.core.session_store import SessionStore
    from apprentice.models.artifact import ArtifactBundle


class _StubGate:
    name = "stub"
    max_retries = 0

    def __init__(
        self, verdict: GateVerdict, *, blocking: bool = True, raises: Exception | None = None
    ) -> None:
        self._verdict = verdict
        self.blocking = blocking
        self._raises = raises
        self.seen: list[tuple[WorkItem, ArtifactBundle]] = []

    def evaluate(self, work_item: WorkItem, artifacts: ArtifactBundle) -> GateResult:
        self.seen.append((work_item, artifacts))
        if self._raises is not None:
            raise self._raises
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


async def _collect(agen: Any) -> list[Any]:
    return [event async for event in agen]


class TestGateAgent:
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

        with pytest.raises(ArtifactError, match="artifact root is a symlink"):
            anyio.run(
                _collect,
                agent._run_async_impl(_StubCtx(state={"generated_code": "x = 1\n"})),
            )
        assert gate.seen == []
        assert list(outside.iterdir()) == []


class _Sentinel(BaseAgent):
    """Records that the pipeline reached the agent after the gate."""

    entered: list[str] = Field(default_factory=list)

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        self.entered.append(self.name)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text="sentinel")]),
        )


async def _drive(
    gates: list[GateAgent], sentinel: _Sentinel
) -> tuple[dict[str, Any], BlockingGateError | None]:
    """Run gates then a sentinel through the real SDK; return STORED state and any halt."""
    service = InMemorySessionService()  # type: ignore[no-untyped-call]
    runner = Runner(
        agent=SequentialAgent(name="pipeline", sub_agents=[*gates, sentinel]),
        app_name="t",
        session_service=service,
    )
    session = await service.create_session(
        app_name="t", user_id="u", state={"generated_code": "x = 1\n"}
    )
    halt: BlockingGateError | None = None
    try:
        async for _event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="go")]),
        ):
            pass
    except BlockingGateError as exc:
        halt = exc
    stored = await service.get_session(app_name="t", user_id="u", session_id=session.id)
    assert stored is not None
    return dict(stored.state), halt


class TestGateAgentInRealPipeline:
    @pytest.mark.parametrize(
        ("verdict", "blocking"),
        [(GateVerdict.PASS, True), (GateVerdict.WARN, True), (GateVerdict.FAIL, False)],
    )
    def test_non_blocking_outcomes_continue_and_persist_the_verdict(
        self, scope: RunScope, verdict: GateVerdict, blocking: bool
    ) -> None:
        tracker = BudgetTracker(total_tokens=1000, total_usd=1.0)
        sentinel = _Sentinel(name="after_gate")
        gate = GateAgent.after(
            _StubGate(verdict, blocking=blocking), "implementation", scope, tracker=tracker
        )

        state, halt = anyio.run(_drive, [gate], sentinel)

        assert halt is None
        assert sentinel.entered == ["after_gate"]
        (stored,) = state["gate_verdicts"]
        assert (stored["verdict"], stored["blocking"]) == (verdict.value, blocking)
        assert stored["diagnostics"] == {"impl": str(scope.work_root / "implementation.py")}
        assert tracker.gate_verdicts == [stored]

    def test_blocking_fail_stops_later_agents_and_persists_diagnostics(
        self, scope: RunScope
    ) -> None:
        tracker = BudgetTracker(total_tokens=1000, total_usd=1.0)
        sentinel = _Sentinel(name="publisher")
        first = GateAgent.after(
            _StubGate(GateVerdict.PASS), "implementation", scope, tracker=tracker
        )
        failing = GateAgent.after(_StubGate(GateVerdict.FAIL), "review", scope, tracker=tracker)

        state, halt = anyio.run(_drive, [first, failing], sentinel)

        assert sentinel.entered == []
        assert halt is not None
        assert [v["verdict"] for v in state["gate_verdicts"]] == ["pass", "fail"]
        assert halt.verdict == state["gate_verdicts"][-1] == tracker.gate_verdicts[-1]
        assert halt.verdict["diagnostics"] == {"impl": str(scope.work_root / "implementation.py")}
        assert str(halt) == "blocking gate failed: stub after review"

    def test_gate_exception_is_a_blocking_failure_with_its_cause(self, scope: RunScope) -> None:
        sentinel = _Sentinel(name="publisher")
        boom = RuntimeError("validator crashed")
        gate = GateAgent.after(_StubGate(GateVerdict.PASS, raises=boom), "review", scope)

        state, halt = anyio.run(_drive, [gate], sentinel)

        assert sentinel.entered == []
        assert halt is not None and halt.__cause__ is boom
        assert state["gate_verdicts"][-1]["diagnostics"] == {
            "error": "gate raised: validator crashed"
        }
        assert state["gate_verdicts"][-1]["blocking"] is True
