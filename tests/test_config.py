"""Tests for config loading and env var interpolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from apprentice.core.config import load_config

_FIXTURES = Path(__file__).parent / "fixtures"
_PROJECT_CONFIG = Path(__file__).parent.parent / "config" / "apprentice.toml"


class TestLoadConfig:
    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/config.toml"))


class TestEnvVarInterpolation:
    def test_env_var_resolved(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("TEST_LOG_PATH", "/tmp/test_logs")
        toml_content = _PROJECT_CONFIG.read_bytes()
        custom = tmp_path / "test.toml"
        custom.write_bytes(
            toml_content.replace(b'"${HOME}/.apprentice/logs"', b'"${TEST_LOG_PATH}"')
        )
        cfg = load_config(custom)
        assert cfg.observability.log_path == "/tmp/test_logs"

    def test_default_value_used(self, tmp_path: Path) -> None:
        toml_content = _PROJECT_CONFIG.read_bytes()
        custom = tmp_path / "test.toml"
        custom.write_bytes(
            toml_content.replace(
                b'"${HOME}/.apprentice/logs"', b'"${NONEXISTENT_VAR:-/fallback/path}"'
            )
        )
        cfg = load_config(custom)
        assert cfg.observability.log_path == "/fallback/path"
