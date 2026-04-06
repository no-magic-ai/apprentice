"""Tests for structured logging and metrics."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from apprentice.core.observability import (
    get_logger,
    log_gate_result,
    log_stage_metrics,
    setup_logging,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestSetupLogging:
    def test_creates_log_directory(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        setup_logging({"log_level": "DEBUG", "log_path": str(log_dir)})
        assert log_dir.exists()

    def test_creates_log_file(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        setup_logging({"log_level": "INFO", "log_path": str(log_dir)})
        logger = get_logger("test")
        logger.info("test message")
        log_file = log_dir / "apprentice.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        record = json.loads(lines[-1])
        assert record["message"] == "test message"
        assert record["level"] == "INFO"

    def test_invalid_level_raises(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(ValueError, match="Unknown log level"):
            setup_logging({"log_level": "INVALID", "log_path": str(tmp_path)})


class TestGetLogger:
    def test_returns_logger(self) -> None:
        logger = get_logger("test.module")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.module"


class TestStageMetrics:
    def test_logs_stage_metrics(self, tmp_path: Path) -> None:
        setup_logging({"log_level": "INFO", "log_path": str(tmp_path)})
        log_stage_metrics(
            stage_name="implementation",
            tokens_used=5000,
            cost_usd=0.045,
            duration_seconds=12.5,
            passed=True,
        )
        log_file = tmp_path / "apprentice.jsonl"
        lines = log_file.read_text().strip().split("\n")
        record = json.loads(lines[-1])
        assert record["extra"]["event"] == "stage_metrics"
        assert record["extra"]["stage_name"] == "implementation"
        assert record["extra"]["tokens_used"] == 5000


class TestGateResult:
    def test_logs_gate_result(self, tmp_path: Path) -> None:
        setup_logging({"log_level": "INFO", "log_path": str(tmp_path)})
        log_gate_result(
            gate_name="correctness",
            verdict="pass",
            diagnostics={"tests_passed": 3},
        )
        log_file = tmp_path / "apprentice.jsonl"
        lines = log_file.read_text().strip().split("\n")
        record = json.loads(lines[-1])
        assert record["extra"]["event"] == "gate_result"
        assert record["extra"]["gate_name"] == "correctness"
