"""Rich progress display for pipeline execution."""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console(stderr=True)

_AGENT_LABELS: dict[str, str] = {
    "apprentice_pipeline": "Pipeline",
    "implementation_loop": "Implementation",
    "drafter": "Drafting code",
    "self_reviewer": "Validating code",
    "artifact_generation": "Generating artifacts",
    "instrumentation": "Instrumenting",
    "visualization": "Creating Manim scene",
    "assessment": "Generating Anki cards",
    "review_loop": "Reviewing artifacts",
    "reviewer": "Running validators",
    "packaging": "Creating PRs",
    "discovery": "Discovering algorithms",
}

_PIPELINE_STAGES = [
    "implementation_loop",
    "artifact_generation",
    "review_loop",
]


def suppress_noisy_loggers() -> None:
    """Suppress LiteLLM, httpx, and other verbose loggers.

    Also removes stderr handlers from the root logger so Rich progress
    display isn't interleaved with log lines. File handler is preserved
    for structured JSON logs.
    """
    for name in ("LiteLLM", "litellm", "httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)

    root = logging.getLogger()
    root.handlers = [
        h
        for h in root.handlers
        if not isinstance(h, logging.StreamHandler)
        or not hasattr(h, "stream")
        or h.stream is not sys.stderr
    ]


class PipelineProgress:
    """Tracks and displays pipeline execution progress using Rich.

    Provides a spinner showing the current agent, a progress bar for
    pipeline stages, and an LLM call counter.
    """

    def __init__(self, algorithm: str, tier: int) -> None:
        self._algorithm = algorithm
        self._tier = tier
        self._llm_calls = 0
        self._current_agent = ""
        self._start_time = time.monotonic()
        self._stage_index = 0
        self._events: list[str] = []
        self._progress = Progress(
            SpinnerColumn("dots"),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=30),
            TextColumn("[dim]{task.fields[detail]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        self._task_id = self._progress.add_task(
            f"Building {algorithm} (tier {tier})",
            total=len(_PIPELINE_STAGES),
            detail="starting...",
        )

    def start(self) -> Progress:
        """Return the Progress instance for use as a context manager."""
        return self._progress

    def on_event(self, event: Any) -> None:
        """Process an ADK event to update the display."""
        author = getattr(event, "author", "")
        if not author or author == "user":
            return

        label = _AGENT_LABELS.get(author, author)

        if author != self._current_agent:
            self._current_agent = author
            if author in _PIPELINE_STAGES:
                self._stage_index = _PIPELINE_STAGES.index(author)
                self._progress.update(
                    self._task_id,
                    completed=self._stage_index,
                    detail=label,
                )
            else:
                self._progress.update(self._task_id, detail=label)

        content = getattr(event, "content", None)
        if content and getattr(content, "parts", None):
            for part in content.parts:
                if getattr(part, "function_call", None):
                    tool_name = part.function_call.name
                    self._progress.update(
                        self._task_id,
                        detail=f"{label} → {tool_name}()",
                    )
                elif getattr(part, "function_response", None) or getattr(part, "text", None):
                    self._llm_calls += 1
                    self._progress.update(
                        self._task_id,
                        detail=f"{label} [{self._llm_calls} LLM calls]",
                    )

    def finish(self, success: bool, elapsed: float) -> None:
        """Mark pipeline as complete and update the display."""
        status = "[bold green]completed" if success else "[bold red]failed"
        self._progress.update(
            self._task_id,
            completed=len(_PIPELINE_STAGES),
            detail=f"{status} in {elapsed:.1f}s ({self._llm_calls} LLM calls)",
        )

    def print_result(self, session_state: dict[str, Any], run_id: str = "") -> None:
        """Print a formatted result summary."""
        table = Table(
            title=f"Build Result: {self._algorithm}", show_header=False, border_style="dim"
        )
        table.add_column("Key", style="bold")
        table.add_column("Value")

        if run_id:
            table.add_row("Run ID", run_id)
        table.add_row("Algorithm", self._algorithm)
        table.add_row("Tier", str(self._tier))

        artifacts = {
            "Implementation": bool(session_state.get("generated_code")),
            "Instrumentation": bool(session_state.get("instrumented_code")),
            "Visualization": bool(session_state.get("manim_scene_code")),
            "Assessment": bool(session_state.get("anki_deck_content")),
        }
        for name, present in artifacts.items():
            icon = "[green]yes[/]" if present else "[red]no[/]"
            table.add_row(name, icon)

        elapsed = time.monotonic() - self._start_time
        table.add_row("Duration", f"{elapsed:.1f}s")
        table.add_row("LLM Calls", str(self._llm_calls))

        console.print(table)


class IntegrationProgress:
    """Tracks progress across multiple algorithm runs."""

    def __init__(self, total: int, backend: str, model: str) -> None:
        self._total = total
        self._completed = 0
        self._succeeded = 0
        self._failed = 0
        self._progress = Progress(
            SpinnerColumn("dots"),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=30),
            TextColumn("{task.fields[detail]}"),
            TextColumn("[dim]{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        )
        self._task_id = self._progress.add_task(
            f"Integration test ({backend}/{model})",
            total=total,
            detail="starting...",
        )

    def start(self) -> Progress:
        return self._progress

    def on_algorithm_start(self, algorithm: str, tier: int) -> None:
        self._progress.update(
            self._task_id,
            detail=f"[yellow]{algorithm}[/] (tier {tier})",
        )

    def on_algorithm_complete(self, algorithm: str, success: bool, elapsed: float) -> None:
        self._completed += 1
        if success:
            self._succeeded += 1
            status = "[green]pass[/]"
        else:
            self._failed += 1
            status = "[red]fail[/]"
        self._progress.update(
            self._task_id,
            completed=self._completed,
            detail=f"{algorithm} {status} ({elapsed:.0f}s)",
        )

    def print_summary(self, report: Any) -> None:
        """Print a formatted summary table."""
        table = Table(title="Integration Test Results", border_style="dim")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")

        rate = report.success_rate * 100
        rate_style = "green" if rate >= 95 else "yellow" if rate >= 80 else "red"
        table.add_row("Success Rate", f"[{rate_style}]{rate:.0f}%[/]")
        table.add_row("Total Runs", str(report.total_runs))
        table.add_row("Passed", f"[green]{report.successful_runs}[/]")
        table.add_row("Failed", f"[red]{report.failed_runs}[/]")
        table.add_row("Total Cost", f"${report.total_cost_usd:.4f}")
        table.add_row("Avg Cost/Algo", f"${report.avg_cost_per_algorithm:.4f}")
        table.add_row("Total Duration", f"{report.total_duration_seconds:.1f}s")

        console.print(table)

        if report.per_agent:
            agent_table = Table(title="Per-Agent Breakdown", border_style="dim")
            agent_table.add_column("Agent", style="bold")
            agent_table.add_column("Calls", justify="right")
            agent_table.add_column("Tokens", justify="right")
            agent_table.add_column("Cost", justify="right")
            for name, metrics in sorted(report.per_agent.items()):
                agent_table.add_row(
                    name,
                    str(metrics.total_calls),
                    f"{metrics.total_tokens:,}",
                    f"${metrics.total_cost_usd:.4f}",
                )
            console.print(agent_table)

        if report.algorithms:
            algo_table = Table(title="Algorithm Results", border_style="dim")
            algo_table.add_column("Algorithm", style="bold")
            algo_table.add_column("Tier", justify="center")
            algo_table.add_column("Status")
            algo_table.add_column("Duration", justify="right")
            for algo in report.algorithms:
                status_str = "[green]pass[/]" if algo["status"] == "completed" else "[red]fail[/]"
                algo_table.add_row(
                    algo["algorithm"],
                    str(algo["tier"]),
                    status_str,
                    f"{algo['elapsed_seconds']:.1f}s",
                )
            console.print(algo_table)
