"""Tests for config loading and env var interpolation."""

from __future__ import annotations

from decimal import Decimal
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


def _variant(tmp_path: Path, old: str, new: str) -> Path:
    text = _PROJECT_CONFIG.read_text(encoding="utf-8")
    assert old in text
    custom = tmp_path / "variant.toml"
    custom.write_text(text.replace(old, new, 1), encoding="utf-8")
    return custom


class TestRemovedSettings:
    @pytest.mark.parametrize(
        ("anchor", "line"),
        [
            ("[provider]\n", 'fallback_model = "openai/gpt-5.4-mini"\n'),
            ("[budget.cycle]\n", "max_algorithms_per_cycle = 3\n"),
            ("[budget.agent]\n", "review_budget_pct = 15\n"),
            ("[agents]\n", "max_review_rounds = 2\n"),
            ("[agents]\n", "max_tool_agent_retries = 1\n"),
            ("[observability]\n", "alert_on_circuit_open = true\n"),
            ("[observability]\n", 'alert_webhook = ""\n'),
            ("[templates]\n", "[gates]\nmax_lint_retries = 2\n\n[templates]\n"),
        ],
    )
    def test_removed_key_is_rejected_with_its_migration(
        self, tmp_path: Path, anchor: str, line: str
    ) -> None:
        new = line if line.startswith("[gates]") else anchor + line
        with pytest.raises(ValueError, match="was removed"):
            load_config(_variant(tmp_path, anchor, new))

    @pytest.mark.parametrize("backend", ["anthropic", "gemini", "ollama", "claude_cli"])
    def test_removed_backend_is_rejected(self, tmp_path: Path, backend: str) -> None:
        with pytest.raises(ValueError, match=f"backend '{backend}' was removed"):
            load_config(_variant(tmp_path, 'backend = "openai"', f'backend = "{backend}"'))


class TestLimitValidation:
    def test_usd_ceilings_are_exact_decimals(self) -> None:
        cfg = load_config(_PROJECT_CONFIG)
        assert cfg.budget.cycle.max_cost_per_cycle_usd == Decimal("5.0")

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("max_tokens_per_stage = 20_000", "max_tokens_per_stage = 0"),
            ("max_prs_per_day = 2", "max_prs_per_day = 0"),
            ("monthly_cost_ceiling_usd = 50.0", "monthly_cost_ceiling_usd = 0"),
            ("cooldown_hours = 4", "cooldown_hours = 0"),
        ],
    )
    def test_zero_ceilings_are_accepted_to_deny_their_scope(
        self, tmp_path: Path, old: str, new: str
    ) -> None:
        load_config(_variant(tmp_path, old, new))

    @pytest.mark.parametrize(
        ("old", "new", "error"),
        [
            ("max_tokens_per_stage = 20_000", "max_tokens_per_stage = -1", ValueError),
            ("max_tokens_per_stage = 20_000", "max_tokens_per_stage = true", TypeError),
            ("max_cost_per_cycle_usd = 5.0", "max_cost_per_cycle_usd = 0.0000000001", ValueError),
            ("max_cost_per_cycle_usd = 5.0", "max_cost_per_cycle_usd = inf", ValueError),
            ("max_concurrent_items = 1", "max_concurrent_items = 0", ValueError),
            ("max_implementation_retries = 3", "max_implementation_retries = 0", ValueError),
            ("failure_threshold = 3", "failure_threshold = 0", ValueError),
            ("tool_agent_budget_pct = 15", "tool_agent_budget_pct = 21", ValueError),
            ("implementation_budget_pct = 40", "implementation_budget_pct = 101", ValueError),
        ],
    )
    def test_out_of_range_limits_are_rejected(
        self, tmp_path: Path, old: str, new: str, error: type[Exception]
    ) -> None:
        with pytest.raises(error):
            load_config(_variant(tmp_path, old, new))

    def test_relative_profile_path_resolves_against_the_config_directory(
        self, tmp_path: Path
    ) -> None:
        custom = _variant(
            tmp_path, 'accounting_profile_path = ""', 'accounting_profile_path = "profile.json"'
        )
        cfg = load_config(custom)
        assert cfg.provider.accounting_profile_path == str(tmp_path / "profile.json")
