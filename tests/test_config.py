"""Tests for config loading and env var interpolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from apprentice.core.config import (
    ApprenticeConfig,
    load_config,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_PROJECT_CONFIG = Path(__file__).parent.parent / "config" / "apprentice.toml"


class TestLoadConfig:
    def test_loads_default_config(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert isinstance(cfg, ApprenticeConfig)

    def test_budget_global(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.budget.global_budget.monthly_token_ceiling == 2_000_000
        assert cfg.budget.global_budget.monthly_cost_ceiling_usd == 50.0

    def test_budget_cycle(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.budget.cycle.max_tokens_per_cycle == 100_000
        assert cfg.budget.cycle.max_algorithms_per_cycle == 3

    def test_budget_stage(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.budget.stage.max_tokens_per_stage == 20_000

    def test_rate_limits(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.rate_limits.max_prs_per_day == 2
        assert cfg.rate_limits.max_prs_per_week == 5
        assert cfg.rate_limits.max_files_per_pr == 10

    def test_gates(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.gates.max_lint_retries == 2
        assert cfg.gates.max_correctness_retries == 1

    def test_circuit_breaker(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.circuit_breaker.failure_threshold == 3

    def test_provider(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.provider.default == "anthropic"
        assert "claude" in cfg.provider.model

    def test_observability(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.observability.log_level == "INFO"
        assert cfg.observability.log_format == "json"

    def test_templates(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.templates.version == "1.0.0"

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
