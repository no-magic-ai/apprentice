"""AgentInterface protocol — contract for all pipeline agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from apprentice.models.agent import AgentContext, AgentResult, AgentTask


@runtime_checkable
class AgentInterface(Protocol):
    """Contract for all pipeline agents.

    Agents receive an AgentTask and return an AgentResult. Each agent owns
    exactly one responsibility (implement, instrument, visualize, etc.) and
    must declare the tools it is permitted to call.
    """

    name: str
    role: str  # Human-readable role description
    system_prompt: str  # Agent's persistent instructions
    allowed_tools: list[str]  # Tools this agent can access

    def execute(
        self,
        task: AgentTask,
        context: AgentContext,
    ) -> AgentResult:
        """Execute the assigned task and return results."""
        ...
