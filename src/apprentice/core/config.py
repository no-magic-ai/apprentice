"""ApprenticeConfig loader — reads config/apprentice.toml with env var interpolation."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Pattern matches ${VAR_NAME} and ${VAR_NAME:-default}
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "apprentice.toml"


def _interpolate(value: str) -> str:
    """Replace ${VAR} and ${VAR:-default} with environment variable values."""

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)  # None if no :- syntax was used
        env_val = os.environ.get(var_name)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        raise KeyError(f"Required environment variable '{var_name}' is not set")

    return _ENV_VAR_RE.sub(_replace, value)


def _interpolate_dict(data: dict[str, object]) -> dict[str, object]:
    """Recursively interpolate env vars in all string values of a dict."""
    result: dict[str, object] = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = _interpolate(value)
        elif isinstance(value, dict):
            result[key] = _interpolate_dict(value)
        else:
            result[key] = value
    return result


def _require(section: dict[str, object], key: str, section_name: str) -> object:
    if key not in section:
        raise ValueError(f"Missing required field '{key}' in [{section_name}]")
    return section[key]


def _require_int(section: dict[str, object], key: str, section_name: str) -> int:
    val = _require(section, key, section_name)
    if not isinstance(val, int):
        raise TypeError(f"[{section_name}].{key} must be an integer, got {type(val).__name__}")
    return val


def _require_float(section: dict[str, object], key: str, section_name: str) -> float:
    val = _require(section, key, section_name)
    if isinstance(val, int):
        return float(val)
    if not isinstance(val, float):
        raise TypeError(f"[{section_name}].{key} must be a number, got {type(val).__name__}")
    return val


def _require_str(section: dict[str, object], key: str, section_name: str) -> str:
    val = _require(section, key, section_name)
    if not isinstance(val, str):
        raise TypeError(f"[{section_name}].{key} must be a string, got {type(val).__name__}")
    return val


def _require_bool(section: dict[str, object], key: str, section_name: str) -> bool:
    val = _require(section, key, section_name)
    if not isinstance(val, bool):
        raise TypeError(f"[{section_name}].{key} must be a boolean, got {type(val).__name__}")
    return val


def _get_section(data: dict[str, object], *keys: str) -> dict[str, object]:
    """Traverse nested dict by keys and return the final value as a section dict."""
    current: object = data
    path = ""
    for key in keys:
        path = f"{path}.{key}" if path else key
        if not isinstance(current, dict):
            raise TypeError(f"Expected a table at '{path}', got {type(current).__name__}")
        if key not in current:
            raise ValueError(f"Missing required section [{path}]")
        current = current[key]
    if not isinstance(current, dict):
        raise TypeError(f"Expected a table at '{'.'.join(keys)}', got {type(current).__name__}")
    return current


@dataclass(frozen=True)
class GlobalBudgetConfig:
    monthly_token_ceiling: int
    monthly_cost_ceiling_usd: float


@dataclass(frozen=True)
class CycleBudgetConfig:
    max_tokens_per_cycle: int
    max_cost_per_cycle_usd: float
    max_algorithms_per_cycle: int


@dataclass(frozen=True)
class StageBudgetConfig:
    max_tokens_per_stage: int


@dataclass(frozen=True)
class BudgetConfig:
    global_budget: GlobalBudgetConfig
    cycle: CycleBudgetConfig
    stage: StageBudgetConfig


@dataclass(frozen=True)
class RateLimitsConfig:
    max_prs_per_day: int
    max_prs_per_week: int
    max_concurrent_items: int
    cooldown_hours: int
    max_files_per_pr: int
    max_lines_per_pr: int


@dataclass(frozen=True)
class GatesConfig:
    max_lint_retries: int
    max_correctness_retries: int
    max_review_rounds: int


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int
    half_open_probe_after_minutes: int
    max_open_cycles_before_manual_reset: int


@dataclass(frozen=True)
class ProviderConfig:
    default: str
    model: str
    fallback_model: str
    fallback_trigger: str


@dataclass(frozen=True)
class ObservabilityConfig:
    log_level: str
    log_format: str
    log_path: str
    metrics_enabled: bool
    alert_on_circuit_open: bool
    alert_webhook: str


@dataclass(frozen=True)
class TemplatesConfig:
    version: str
    base_path: str


@dataclass(frozen=True)
class ApprenticeConfig:
    budget: BudgetConfig
    rate_limits: RateLimitsConfig
    gates: GatesConfig
    circuit_breaker: CircuitBreakerConfig
    provider: ProviderConfig
    observability: ObservabilityConfig
    templates: TemplatesConfig


def _parse_budget(data: dict[str, object]) -> BudgetConfig:
    raw = _get_section(data, "budget")
    global_raw = _get_section(raw, "global")
    cycle_raw = _get_section(raw, "cycle")
    stage_raw = _get_section(raw, "stage")

    global_budget = GlobalBudgetConfig(
        monthly_token_ceiling=_require_int(global_raw, "monthly_token_ceiling", "budget.global"),
        monthly_cost_ceiling_usd=_require_float(
            global_raw, "monthly_cost_ceiling_usd", "budget.global"
        ),
    )
    cycle = CycleBudgetConfig(
        max_tokens_per_cycle=_require_int(cycle_raw, "max_tokens_per_cycle", "budget.cycle"),
        max_cost_per_cycle_usd=_require_float(cycle_raw, "max_cost_per_cycle_usd", "budget.cycle"),
        max_algorithms_per_cycle=_require_int(
            cycle_raw, "max_algorithms_per_cycle", "budget.cycle"
        ),
    )
    stage = StageBudgetConfig(
        max_tokens_per_stage=_require_int(stage_raw, "max_tokens_per_stage", "budget.stage"),
    )
    return BudgetConfig(global_budget=global_budget, cycle=cycle, stage=stage)


def _parse_rate_limits(data: dict[str, object]) -> RateLimitsConfig:
    raw = _get_section(data, "rate_limits")
    return RateLimitsConfig(
        max_prs_per_day=_require_int(raw, "max_prs_per_day", "rate_limits"),
        max_prs_per_week=_require_int(raw, "max_prs_per_week", "rate_limits"),
        max_concurrent_items=_require_int(raw, "max_concurrent_items", "rate_limits"),
        cooldown_hours=_require_int(raw, "cooldown_hours", "rate_limits"),
        max_files_per_pr=_require_int(raw, "max_files_per_pr", "rate_limits"),
        max_lines_per_pr=_require_int(raw, "max_lines_per_pr", "rate_limits"),
    )


def _parse_gates(data: dict[str, object]) -> GatesConfig:
    raw = _get_section(data, "gates")
    return GatesConfig(
        max_lint_retries=_require_int(raw, "max_lint_retries", "gates"),
        max_correctness_retries=_require_int(raw, "max_correctness_retries", "gates"),
        max_review_rounds=_require_int(raw, "max_review_rounds", "gates"),
    )


def _parse_circuit_breaker(data: dict[str, object]) -> CircuitBreakerConfig:
    raw = _get_section(data, "circuit_breaker")
    return CircuitBreakerConfig(
        failure_threshold=_require_int(raw, "failure_threshold", "circuit_breaker"),
        half_open_probe_after_minutes=_require_int(
            raw, "half_open_probe_after_minutes", "circuit_breaker"
        ),
        max_open_cycles_before_manual_reset=_require_int(
            raw, "max_open_cycles_before_manual_reset", "circuit_breaker"
        ),
    )


def _parse_provider(data: dict[str, object]) -> ProviderConfig:
    raw = _get_section(data, "provider")
    return ProviderConfig(
        default=_require_str(raw, "default", "provider"),
        model=_require_str(raw, "model", "provider"),
        fallback_model=_require_str(raw, "fallback_model", "provider"),
        fallback_trigger=_require_str(raw, "fallback_trigger", "provider"),
    )


def _parse_observability(data: dict[str, object]) -> ObservabilityConfig:
    raw = _get_section(data, "observability")
    return ObservabilityConfig(
        log_level=_require_str(raw, "log_level", "observability"),
        log_format=_require_str(raw, "log_format", "observability"),
        log_path=_require_str(raw, "log_path", "observability"),
        metrics_enabled=_require_bool(raw, "metrics_enabled", "observability"),
        alert_on_circuit_open=_require_bool(raw, "alert_on_circuit_open", "observability"),
        alert_webhook=_require_str(raw, "alert_webhook", "observability"),
    )


def _parse_templates(data: dict[str, object]) -> TemplatesConfig:
    raw = _get_section(data, "templates")
    return TemplatesConfig(
        version=_require_str(raw, "version", "templates"),
        base_path=_require_str(raw, "base_path", "templates"),
    )


def load_config(path: Path | None = None) -> ApprenticeConfig:
    """Load and validate config from a TOML file.

    Defaults to config/apprentice.toml relative to the project root.
    Raises ValueError on missing required fields, TypeError on wrong types,
    KeyError on unresolved required environment variables.
    """
    config_path = path if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("rb") as f:
        raw: dict[str, object] = tomllib.load(f)

    data = _interpolate_dict(raw)

    return ApprenticeConfig(
        budget=_parse_budget(data),
        rate_limits=_parse_rate_limits(data),
        gates=_parse_gates(data),
        circuit_breaker=_parse_circuit_breaker(data),
        provider=_parse_provider(data),
        observability=_parse_observability(data),
        templates=_parse_templates(data),
    )
