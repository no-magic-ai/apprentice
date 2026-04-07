"""Claude CLI model — wraps `claude -p` as an ADK-compatible model.

Uses the Claude Code CLI subscription instead of a direct API key.
Text-only: supports generation agents but not tool-calling agents.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.models.llm_request import LlmRequest


class ClaudeCli(BaseLlm):
    """ADK model that calls `claude -p --no-session-persistence` via subprocess.

    Converts LlmRequest contents to a text prompt, runs the CLI, and wraps
    the output in an LlmResponse. Supports text generation only — agents
    with tool calling must use a different model.
    """

    model: str = "claude-cli"

    def _build_command(self) -> list[str]:
        """Build the claude CLI command with optional model flag."""
        cmd = ["claude", "-p", "--no-session-persistence"]
        if self.model and self.model != "claude-cli":
            cmd.extend(["--model", self.model])
        return cmd

    def _contents_to_prompt(self, llm_request: LlmRequest) -> str:
        """Extract a text prompt from LlmRequest contents."""
        parts: list[str] = []

        if llm_request.config and hasattr(llm_request.config, "system_instruction"):
            si = llm_request.config.system_instruction
            if si:
                if isinstance(si, str):
                    parts.append(f"[System]\n{si}\n")
                elif hasattr(si, "parts") and si.parts:
                    for p in si.parts:
                        if hasattr(p, "text") and p.text:
                            parts.append(f"[System]\n{p.text}\n")

        for content in llm_request.contents or []:
            role = getattr(content, "role", "user")
            for part in getattr(content, "parts", []):
                text = getattr(part, "text", None)
                if text:
                    parts.append(f"[{role}]\n{text}\n")

        return "\n".join(parts)

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """Generate content by calling claude CLI.

        Args:
            llm_request: The ADK request with conversation contents.
            stream: Ignored — CLI always returns complete responses.

        Yields:
            A single LlmResponse with the CLI output.
        """
        prompt = self._contents_to_prompt(llm_request)

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                self._build_command(),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = result.stdout.strip()
            if result.returncode != 0 and not output:
                output = (
                    f"Error: claude CLI returned code {result.returncode}: {result.stderr[:300]}"
                )
        except FileNotFoundError:
            output = "Error: claude CLI not found. Install Claude Code: https://docs.anthropic.com/en/docs/claude-code"
        except subprocess.TimeoutExpired:
            output = "Error: claude CLI timed out after 300s"

        response_content = types.Content(
            role="model",
            parts=[types.Part(text=output)],
        )

        yield LlmResponse(
            content=response_content,
            turn_complete=True,
        )
