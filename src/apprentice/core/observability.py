"""Structured logging, metrics, and alerting."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_METRICS_LOGGER_NAME = "apprentice.metrics"


class _JsonFormatter(logging.Formatter):
    """Formats log records as newline-delimited JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        extra: dict[str, Any] = {}
        skip = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in skip:
                extra[key] = value

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "extra": extra,
        }

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class _HumanFormatter(logging.Formatter):
    """Human-readable formatter for stderr."""

    _FMT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    _DATEFMT = "%Y-%m-%dT%H:%M:%S"

    def __init__(self) -> None:
        super().__init__(fmt=self._FMT, datefmt=self._DATEFMT)


def setup_logging(config: dict[str, Any]) -> None:
    """Configure root logger with JSON file handler and human-readable stderr handler.

    Args:
        config: Mapping that must contain ``log_level`` (str) and ``log_path`` (str).
                ``log_path`` supports ``${HOME}``-style environment-variable expansion.
    """
    raw_level: str = config.get("log_level", "INFO")
    level = logging.getLevelName(raw_level.upper())
    if not isinstance(level, int):
        raise ValueError(f"Unknown log level: {raw_level!r}")

    raw_path: str = config.get("log_path", "${HOME}/.apprentice/logs")
    expanded_path = os.path.expandvars(raw_path)
    log_dir = Path(expanded_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "apprentice.jsonl"

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(_JsonFormatter())
    root.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(_HumanFormatter())
    root.addHandler(stderr_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.

    Args:
        name: Logger name, typically ``__name__`` of the calling module.

    Returns:
        A ``logging.Logger`` instance.
    """
    return logging.getLogger(name)


def log_stage_metrics(
    stage_name: str,
    tokens_used: int,
    cost_usd: float,
    duration_seconds: float,
    passed: bool,
) -> None:
    """Log a structured metrics event for a stage execution.

    Args:
        stage_name: Identifier of the pipeline stage (e.g. ``"implementation"``).
        tokens_used: Total tokens consumed during the stage.
        cost_usd: Estimated USD cost of the stage.
        duration_seconds: Wall-clock time the stage took to run.
        passed: Whether the stage completed successfully.
    """
    logger = logging.getLogger(_METRICS_LOGGER_NAME)
    logger.info(
        "stage_metrics",
        extra={
            "event": "stage_metrics",
            "stage_name": stage_name,
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
            "duration_seconds": duration_seconds,
            "passed": passed,
        },
    )


def log_gate_result(
    gate_name: str,
    verdict: str,
    diagnostics: dict[str, Any],
) -> None:
    """Log a structured metrics event for a gate evaluation.

    Args:
        gate_name: Identifier of the gate (e.g. ``"lint"``, ``"correctness"``).
        verdict: Outcome string, typically ``"pass"`` or ``"fail"``.
        diagnostics: Arbitrary key-value data produced by the gate.
    """
    logger = logging.getLogger(_METRICS_LOGGER_NAME)
    logger.info(
        "gate_result",
        extra={
            "event": "gate_result",
            "gate_name": gate_name,
            "verdict": verdict,
            "diagnostics": diagnostics,
        },
    )
