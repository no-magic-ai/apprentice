"""Tests for structured logging and metrics."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from apprentice.core.observability import (
    get_logger,
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
