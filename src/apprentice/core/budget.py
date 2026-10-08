"""Run budget summary: ordered gate verdicts plus the cycle's ledger reference.

Token and cost accounting happens at the bound model client against the
installation ledger (`apprentice.controls`), never here; a run record keeps
only this derived summary of what the ledger holds for its cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BudgetTracker:
    """Collects the gate verdicts of one run in execution order.

    Attributes:
        gate_verdicts: Ordered list of gate results appended in execution order.
    """

    gate_verdicts: list[dict[str, Any]] = field(default_factory=list)

    def record_gate_verdict(
        self,
        gate_name: str,
        after_stage: str,
        verdict: str,
        *,
        blocking: bool,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        """Append a gate verdict in execution order, noting whether the gate blocks."""
        self.gate_verdicts.append(
            {
                "gate_name": gate_name,
                "after_stage": after_stage,
                "verdict": verdict,
                "blocking": blocking,
                "diagnostics": diagnostics or {},
            }
        )

    def to_dict(self, accounting: dict[str, Any]) -> dict[str, Any]:
        """Return the run's budget summary with its cycle's ledger reference."""
        return {"gate_verdicts": self.gate_verdicts, "accounting": accounting}
