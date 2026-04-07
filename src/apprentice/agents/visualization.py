"""Visualization Agent — ADK LlmAgent that generates Manim animation scenes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_SCAFFOLD_TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent.parent / "config" / "templates" / "manim_scene.py.j2"
)

_INSTRUCTION = """\
You are an expert Manim animator for the no-magic educational project.
Your task is to generate a complete Manim animation scene that visualizes
an algorithm's execution.

The implementation code is available at: {implementation_path}

Animation rules:
- Each animation step must correspond to a semantically meaningful algorithmic event
  (comparison, swap, assignment, recursive call, etc.).
- Use only standard Manim objects and methods.
- Animations must be deterministic and reproducible — no random elements.
- Keep scenes concise: prefer Transform and Indicate over constructing new objects.
- Include a brief text label for each major phase of the algorithm.
- Use self.wait(0.5) between major steps and self.wait(1) at the end.
- The class must inherit from Scene and implement a construct() method.

If a scaffold template is available via the load_manim_template tool, use it
as the base structure and fill in the construct() method.

Write the complete Manim Python source code. Do NOT use markdown fences.
Write ONLY the Python source code, nothing else.
"""


def load_manim_template() -> dict[str, Any]:
    """Load the Manim scene scaffold template if available.

    Returns:
        Dict with 'available' bool and 'template' string content.
    """
    if _SCAFFOLD_TEMPLATE_PATH.exists():
        content = _SCAFFOLD_TEMPLATE_PATH.read_text(encoding="utf-8")
        return {"available": True, "template": content}
    return {"available": False, "template": ""}


def build_visualization_agent(model: LiteLlm) -> LlmAgent:
    """Build an ADK LlmAgent for Manim visualization generation.

    Reads {implementation_path} from session state and produces
    a Manim Scene script.

    Args:
        model: LiteLlm model instance.

    Returns:
        A configured LlmAgent with the load_manim_template tool.
    """
    return LlmAgent(
        name="visualization",
        model=model,
        instruction=_INSTRUCTION,
        tools=[load_manim_template],
        output_key="manim_scene_code",
        description="Generates Manim animation scenes for algorithm visualization.",
    )
