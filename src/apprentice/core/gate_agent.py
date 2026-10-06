"""GateAgent — ADK BaseAgent wrapper that runs a gates/ check between stages.

Bridges `apprentice.gates.base.GateInterface` implementations into the ADK
`SequentialAgent` pipeline so deterministic post-stage gates actually fire at
runtime. A blocking FAIL yields an event with `ctx.end_invocation = True`
halting the pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.adk.agents import BaseAgent

from apprentice.core.artifacts import (
    RunScope,
    artifact_bundle,
    write_state_roles,
)
from apprentice.core.budget import BudgetTracker  # noqa: TC001 — pydantic needs at runtime
from apprentice.core.observability import get_logger
from apprentice.models.work_item import GateVerdict

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events import Event

    from apprentice.gates.base import GateInterface

_logger = get_logger(__name__)


class GateAgent(BaseAgent):
    """ADK agent that runs a `GateInterface` as a deterministic pipeline gate.

    On PASS it yields a small confirmation event and the pipeline continues.
    On a blocking FAIL it sets `ctx.end_invocation = True` and yields a FAIL
    event — downstream sub-agents will not run. WARN logs a warning and
    continues.

    Gate verdicts are recorded into `state['gate_verdicts']` (ordered list)
    and into the shared `BudgetTracker` when one is provided.

    Session-state outputs are written into the run's own work root (gates exec
    files, parse CSVs, etc.) and the gate sees the run's identity, never a
    shared temporary directory or an identity rebuilt from state.
    """

    gate: Any
    after_stage: str
    scope: RunScope
    tracker: BudgetTracker | None = None

    @classmethod
    def after(
        cls,
        gate: GateInterface,
        after_stage: str,
        scope: RunScope,
        tracker: BudgetTracker | None = None,
    ) -> GateAgent:
        """Build the gate agent that evaluates `gate` after `after_stage` for `scope`."""
        return cls(
            name=f"gate_{gate.name}_after_{after_stage}",
            description=f"Gate '{gate.name}' evaluated after stage '{after_stage}'.",
            gate=gate,
            after_stage=after_stage,
            scope=scope,
            tracker=tracker,
        )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        from google.adk.events import Event
        from google.genai import types

        state = dict(ctx.session.state)
        work_item = self.scope.work_item()
        bundle = artifact_bundle(self.scope.run_id, write_state_roles(self.scope.work_root, state))

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

        is_blocking_fail = verdict_value == GateVerdict.FAIL.value and getattr(
            self.gate, "blocking", True
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
