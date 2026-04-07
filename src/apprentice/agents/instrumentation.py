"""Instrumentation Agent — ADK LlmAgent that adds trace hooks to implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import LlmAgent

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_INSTRUCTION = """\
You are an expert in algorithm instrumentation for the no-magic educational project.
Your task is to add trace hooks to a clean algorithm implementation so that learners
can observe each meaningful step as it executes.

The original implementation code is provided in the conversation history as
the generated_code output from the drafter agent.

Instrumentation rules:
- Never alter the algorithm's correctness or time complexity class.
- Emit trace events as JSON dicts with keys: "step", "operation", "state".
- Place hooks at semantically meaningful points: comparisons, swaps,
  assignments, recursive calls, and loop iterations that change state.
- Keep the original code structure intact — do not refactor or rename symbols.
- All trace calls must be guarded: only emit when a `trace` callable is provided.
- Preserve all existing type annotations and docstrings.
- Accept an optional `trace` parameter (callable or None) on the main function.

Write the complete instrumented Python source code. Do NOT use markdown fences.
Write ONLY the Python source code, nothing else.
"""


def build_instrumentation_agent(model: LiteLlm) -> LlmAgent:
    """Build an ADK LlmAgent for algorithm instrumentation.

    Reads the generated code from conversation history and produces
    instrumented code with trace hooks.

    Args:
        model: LiteLlm model instance.

    Returns:
        A configured LlmAgent.
    """
    return LlmAgent(
        name="instrumentation",
        model=model,
        instruction=_INSTRUCTION,
        output_key="instrumented_code",
        description="Adds trace hooks to algorithm implementations for step-by-step observation.",
    )
