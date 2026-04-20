"""GateAgent — ADK BaseAgent wrapper that runs a gates/ check between stages.

Bridges `apprentice.gates.base.GateInterface` implementations into the ADK
`SequentialAgent` pipeline so deterministic post-stage gates actually fire at
runtime. A blocking FAIL yields an event with `ctx.end_invocation = True`
halting the pipeline.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from google.adk.agents import BaseAgent

from apprentice.core.budget import BudgetTracker  # noqa: TC001 — pydantic needs at runtime
from apprentice.core.observability import get_logger
from apprentice.models.artifact import ArtifactBundle
from apprentice.models.work_item import GateVerdict, WorkItem, WorkItemStatus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events import Event

    from apprentice.gates.base import GateInterface

_logger = get_logger(__name__)


def materialize_artifacts(state: dict[str, Any]) -> ArtifactBundle:
    """Materialize session-state outputs to disk and populate an ArtifactBundle.

    Gates expect on-disk paths (they exec files, parse CSVs, etc.). ADK stores
    outputs as strings in session state, so the gate boundary is where we
    persist them.
    """
    algorithm_name = state.get("algorithm_name", "algorithm")
    tmp_dir = Path(tempfile.gettempdir()) / "apprentice_artifacts"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    bundle = ArtifactBundle(id=algorithm_name, work_item_id=algorithm_name)

    impl = state.get("generated_code", "")
    if impl:
        p = tmp_dir / f"{algorithm_name}.py"
        p.write_text(impl, encoding="utf-8")
        bundle.implementation_path = str(p)

    instr = state.get("instrumented_code", "")
    if instr:
        p = tmp_dir / f"{algorithm_name}_instrumented.py"
        p.write_text(instr, encoding="utf-8")
        bundle.instrumented_path = str(p)

    scene = state.get("manim_scene_code", "")
    if scene:
        p = tmp_dir / f"{algorithm_name}_scene.py"
        p.write_text(scene, encoding="utf-8")
        bundle.manim_scene_path = str(p)

    anki = state.get("anki_deck_content", "")
    if anki:
        p = tmp_dir / f"{algorithm_name}_cards.csv"
        p.write_text(anki, encoding="utf-8")
        bundle.anki_deck_path = str(p)

    return bundle


def _work_item_from_state(state: dict[str, Any]) -> WorkItem:
    """Build a WorkItem from session state for gate evaluation."""
    return WorkItem(
        id=str(state.get("algorithm_name", "algorithm")),
        algorithm_name=str(state.get("algorithm_name", "algorithm")),
        tier=int(state.get("algorithm_tier", 2)),
        status=WorkItemStatus.IN_PROGRESS,
    )


class GateAgent(BaseAgent):
    """ADK agent that runs a `GateInterface` as a deterministic pipeline gate.

    On PASS it yields a small confirmation event and the pipeline continues.
    On a blocking FAIL it sets `ctx.end_invocation = True` and yields a FAIL
    event — downstream sub-agents will not run. WARN logs a warning and
    continues.

    Gate verdicts are recorded into `state['gate_verdicts']` (ordered list)
    and into the shared `BudgetTracker` when one is provided.
    """

    model_config: ClassVar[dict[str, Any]] = {"arbitrary_types_allowed": True}

    gate: Any
    after_stage: str
    tracker: BudgetTracker | None = None

    def __init__(
        self,
        gate: GateInterface,
        after_stage: str,
        tracker: BudgetTracker | None = None,
    ) -> None:
        super().__init__(
            name=f"gate_{gate.name}_after_{after_stage}",
            description=f"Gate '{gate.name}' evaluated after stage '{after_stage}'.",
            gate=gate,
            after_stage=after_stage,
            tracker=tracker,
        )

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        from google.adk.events import Event
        from google.genai import types

        state = dict(ctx.session.state)
        work_item = _work_item_from_state(state)
        bundle = materialize_artifacts(state)

        try:
            result = self.gate.evaluate(work_item, bundle)
            verdict_value = result.verdict.value
            diagnostics = result.diagnostics
        except Exception as exc:
            _logger.exception(
                "gate_exception",
                extra={"gate_name": self.gate.name, "after_stage": self.after_stage},
            )
            verdict_value = GateVerdict.FAIL.value
            diagnostics = {"error": f"gate raised: {exc}"}

        verdict_entry = {
            "gate_name": self.gate.name,
            "after_stage": self.after_stage,
            "verdict": verdict_value,
            "diagnostics": diagnostics,
        }

        verdicts: list[dict[str, Any]] = list(ctx.session.state.get("gate_verdicts", []))
        verdicts.append(verdict_entry)
        ctx.session.state["gate_verdicts"] = verdicts

        if self.tracker is not None:
            self.tracker.record_gate_verdict(
                gate_name=self.gate.name,
                after_stage=self.after_stage,
                verdict=verdict_value,
                diagnostics=diagnostics,
            )

        is_blocking_fail = (
            verdict_value == GateVerdict.FAIL.value and getattr(self.gate, "blocking", True)
        )

        if is_blocking_fail:
            _logger.error(
                "blocking_gate_failed",
                extra={
                    "gate_name": self.gate.name,
                    "after_stage": self.after_stage,
                    "diagnostics": diagnostics,
                },
            )
            ctx.end_invocation = True
            message = f"Gate '{self.gate.name}' FAILED after {self.after_stage}"
        elif verdict_value == GateVerdict.WARN.value:
            _logger.warning(
                "gate_warning",
                extra={
                    "gate_name": self.gate.name,
                    "after_stage": self.after_stage,
                    "diagnostics": diagnostics,
                },
            )
            message = f"Gate '{self.gate.name}' WARN after {self.after_stage}"
        else:
            message = f"Gate '{self.gate.name}' PASS after {self.after_stage}"

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=getattr(ctx, "branch", None),
            content=types.Content(
                role="model",
                parts=[types.Part(text=message)],
            ),
        )
