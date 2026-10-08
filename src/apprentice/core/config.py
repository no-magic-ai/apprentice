"""ApprenticeConfig loader — reads config/apprentice.toml with env var interpolation."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Overflow
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


_NANODOLLARS_PER_USD = Decimal(1_000_000_000)

# Keys that earlier releases accepted but no consumer ever enforced. They are
# rejected with their migration rather than silently ignored.
_REMOVED_KEYS: dict[tuple[str, str], str] = {
    ("provider", "fallback_model"): "apprentice never switches models; delete the key",
    ("budget.cycle", "max_algorithms_per_cycle"): "one cycle builds one algorithm; delete the key",
    ("budget.agent", "review_budget_pct"): (
        "artifact review is programmatic and makes no model call; delete the key"
    ),
    ("agents", "max_review_rounds"): (
        "artifact review is programmatic and runs once; delete the key"
    ),
    ("agents", "max_tool_agent_retries"): "tool agents run once per cycle; delete the key",
    ("gates", "max_lint_retries"): "gates run once; delete the [gates] section",
    ("gates", "max_correctness_retries"): "gates run once; delete the [gates] section",
    ("gates", "max_review_rounds"): "gates run once; delete the [gates] section",
    ("observability", "alert_on_circuit_open"): "no alert transport exists; delete the key",
    ("observability", "alert_webhook"): "no alert transport exists; delete the key",
}

SUPPORTED_BACKENDS = ("local", "openai")
_REMOVED_BACKENDS: dict[str, str] = {
    "anthropic": "its usage and fees are not metered by a qualified accounting profile",
    "gemini": "its usage and fees are not metered by a qualified accounting profile",
    "ollama": (
        "configure a genuinely non-hosted server that implements the counted Responses "
        "protocol as backend 'local' with a non-hosted accounting profile"
    ),
    "claude_cli": "it is an unmetered hosted subscription process, not a factory model",
}


def backend_problem(backend: str) -> str | None:
    """Return why `backend` cannot be used, or None if it is supported."""
    if backend in SUPPORTED_BACKENDS:
        return None
    if backend in _REMOVED_BACKENDS:
        return (
            f"backend {backend!r} was removed: {_REMOVED_BACKENDS[backend]}; "
            f"supported backends: {', '.join(SUPPORTED_BACKENDS)}"
        )
    return f"unsupported backend {backend!r}; supported backends: {', '.join(SUPPORTED_BACKENDS)}"


def _reject_removed_keys(data: dict[str, object]) -> None:
    found: list[str] = []
    for (section_path, key), migration in _REMOVED_KEYS.items():
        current: object = data
        for part in section_path.split("."):
            current = current.get(part) if isinstance(current, dict) else None
        if isinstance(current, dict) and key in current:
            found.append(f"[{section_path}].{key} was removed: {migration}")
    gates = data.get("gates")
    if isinstance(gates, dict) and not gates:
        found.append("[gates] was removed: gates run once; delete the section")
    if found:
        raise ValueError("unsupported configuration: " + "; ".join(found))


def _require(section: dict[str, object], key: str, section_name: str) -> object:
    if key not in section:
        raise ValueError(f"Missing required field '{key}' in [{section_name}]")
    return section[key]


def _require_int(section: dict[str, object], key: str, section_name: str) -> int:
    val = _require(section, key, section_name)
    if not isinstance(val, int) or isinstance(val, bool):
        raise TypeError(f"[{section_name}].{key} must be an integer, got {type(val).__name__}")
    return val


def _require_count(section: dict[str, object], key: str, section_name: str, minimum: int) -> int:
    val = _require_int(section, key, section_name)
    if val < minimum:
        raise ValueError(f"[{section_name}].{key} must be at least {minimum}, got {val}")
    return val


def _require_decimal(section: dict[str, object], key: str, section_name: str) -> Decimal:
    """Return an exact finite nonnegative number (TOML floats are parsed as Decimal)."""
    val = _require(section, key, section_name)
    if isinstance(val, bool) or not isinstance(val, (int, Decimal)):
        raise TypeError(f"[{section_name}].{key} must be a number, got {type(val).__name__}")
    number = Decimal(val)
    if not number.is_finite() or number < 0:
        raise ValueError(f"[{section_name}].{key} must be a finite nonnegative number")
    return number


HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
_MICROSECOND = timedelta(microseconds=1)


def delay(value: Decimal, unit: timedelta) -> timedelta:
    """`value` units of `unit` as an exact timedelta (truncated to whole microseconds).

    A delay is added to the ledger's UTC clock, so it must keep that deadline
    representable: the latest representable UTC instant (`datetime.max`,
    year 9999) bounds it from the current time. The exact microsecond count
    is checked against that bound before it becomes an integer, so a huge
    exponent is refused at once instead of being expanded first.

    Raises:
        ValueError: If the delay or a deadline that far from now cannot be
            represented.
    """
    now = datetime.now(tz=UTC)
    latest = (datetime.max.replace(tzinfo=UTC) - now) // _MICROSECOND
    beyond = (
        f"{value} x {unit} puts a deadline beyond the latest representable time "
        f"({datetime.max.year})"
    )
    try:
        microseconds = value * (unit // _MICROSECOND)
    except Overflow as exc:
        raise ValueError(beyond) from exc
    if microseconds > latest:
        raise ValueError(beyond)
    return timedelta(microseconds=int(microseconds))


def _require_delay(
    section: dict[str, object], key: str, section_name: str, unit: timedelta
) -> Decimal:
    number = _require_decimal(section, key, section_name)
    try:
        delay(number, unit)
    except ValueError as exc:
        raise ValueError(f"[{section_name}].{key} is too large: {exc}") from exc
    return number


def _require_usd(section: dict[str, object], key: str, section_name: str) -> Decimal:
    usd = _require_decimal(section, key, section_name)
    try:
        nanodollars = usd * _NANODOLLARS_PER_USD
    except Overflow as exc:
        raise ValueError(
            f"[{section_name}].{key} is too large to be counted in nanodollars: {usd}"
        ) from exc
    if nanodollars != nanodollars.to_integral_value():
        raise ValueError(f"[{section_name}].{key} must be a whole number of nanodollars")
    return usd


def _require_percentage(section: dict[str, object], key: str, section_name: str) -> Decimal:
    pct = _require_decimal(section, key, section_name)
    if pct > 100:
        raise ValueError(f"[{section_name}].{key} must be at most 100, got {pct}")
    return pct


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
    monthly_cost_ceiling_usd: Decimal


@dataclass(frozen=True)
class CycleBudgetConfig:
    max_tokens_per_cycle: int
    max_cost_per_cycle_usd: Decimal


@dataclass(frozen=True)
class StageBudgetConfig:
    max_tokens_per_stage: int


@dataclass(frozen=True)
class AgentBudgetConfig:
    max_tokens_per_agent_call: int
    implementation_budget_pct: Decimal
    tool_agent_budget_pct: Decimal


@dataclass(frozen=True)
class BudgetConfig:
    global_budget: GlobalBudgetConfig
    cycle: CycleBudgetConfig
    stage: StageBudgetConfig
    agent: AgentBudgetConfig


@dataclass(frozen=True)
class RateLimitsConfig:
    max_prs_per_day: int
    max_prs_per_week: int
    max_concurrent_items: int
    cooldown_hours: Decimal
    max_files_per_pr: int
    max_lines_per_pr: int


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int
    half_open_probe_after_minutes: Decimal
    max_open_cycles_before_manual_reset: int


@dataclass(frozen=True)
class ProviderConfig:
    """Model route: backend, model and the accounting profile qualifying them.

    `accounting_profile_path` is the absolute path of the version-1 accounting
    profile (relative values resolve against the config file's directory), or
    "" when none is configured, in which case every model call is denied
    before any request.
    """

    backend: str
    model: str
    local_api_base: str
    accounting_profile_path: str


@dataclass(frozen=True)
class ObservabilityConfig:
    log_level: str
    log_format: str
    log_path: str
    metrics_enabled: bool


@dataclass(frozen=True)
class TemplatesConfig:
    version: str
    base_path: str


@dataclass(frozen=True)
class AgentsConfig:
    max_implementation_retries: int


@dataclass(frozen=True)
class ApprenticeConfig:
    budget: BudgetConfig
    rate_limits: RateLimitsConfig
    agents: AgentsConfig
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
        monthly_token_ceiling=_require_count(
            global_raw, "monthly_token_ceiling", "budget.global", 0
        ),
        monthly_cost_ceiling_usd=_require_usd(
            global_raw, "monthly_cost_ceiling_usd", "budget.global"
        ),
    )
    cycle = CycleBudgetConfig(
        max_tokens_per_cycle=_require_count(cycle_raw, "max_tokens_per_cycle", "budget.cycle", 0),
        max_cost_per_cycle_usd=_require_usd(cycle_raw, "max_cost_per_cycle_usd", "budget.cycle"),
    )
    stage = StageBudgetConfig(
        max_tokens_per_stage=_require_count(stage_raw, "max_tokens_per_stage", "budget.stage", 0),
    )
    agent_raw = _get_section(raw, "agent")
    agent = AgentBudgetConfig(
        max_tokens_per_agent_call=_require_count(
            agent_raw, "max_tokens_per_agent_call", "budget.agent", 0
        ),
        implementation_budget_pct=_require_percentage(
            agent_raw, "implementation_budget_pct", "budget.agent"
        ),
        tool_agent_budget_pct=_require_percentage(
            agent_raw, "tool_agent_budget_pct", "budget.agent"
        ),
    )
    allocated = agent.implementation_budget_pct + 3 * agent.tool_agent_budget_pct
    if allocated > 100:
        raise ValueError(
            "[budget.agent] implementation_budget_pct plus three tool_agent_budget_pct "
            f"allocations must not exceed 100, got {allocated}"
        )
    return BudgetConfig(global_budget=global_budget, cycle=cycle, stage=stage, agent=agent)


def _parse_rate_limits(data: dict[str, object]) -> RateLimitsConfig:
    raw = _get_section(data, "rate_limits")
    return RateLimitsConfig(
        max_prs_per_day=_require_count(raw, "max_prs_per_day", "rate_limits", 0),
        max_prs_per_week=_require_count(raw, "max_prs_per_week", "rate_limits", 0),
        max_concurrent_items=_require_count(raw, "max_concurrent_items", "rate_limits", 1),
        cooldown_hours=_require_delay(raw, "cooldown_hours", "rate_limits", HOUR),
        max_files_per_pr=_require_count(raw, "max_files_per_pr", "rate_limits", 0),
        max_lines_per_pr=_require_count(raw, "max_lines_per_pr", "rate_limits", 0),
    )


def _parse_agents(data: dict[str, object]) -> AgentsConfig:
    raw = _get_section(data, "agents")
    return AgentsConfig(
        max_implementation_retries=_require_count(raw, "max_implementation_retries", "agents", 1),
    )


def _parse_circuit_breaker(data: dict[str, object]) -> CircuitBreakerConfig:
    raw = _get_section(data, "circuit_breaker")
    return CircuitBreakerConfig(
        failure_threshold=_require_count(raw, "failure_threshold", "circuit_breaker", 1),
        half_open_probe_after_minutes=_require_delay(
            raw, "half_open_probe_after_minutes", "circuit_breaker", MINUTE
        ),
        max_open_cycles_before_manual_reset=_require_count(
            raw, "max_open_cycles_before_manual_reset", "circuit_breaker", 1
        ),
    )


def _parse_provider(data: dict[str, object], config_dir: Path) -> ProviderConfig:
    raw = _get_section(data, "provider")
    backend = _require_str(raw, "backend", "provider")
    problem = backend_problem(backend)
    if problem:
        raise ValueError(f"[provider].backend: {problem}")
    profile = _require_str(raw, "accounting_profile_path", "provider")
    if profile:
        profile = str((config_dir / Path(profile).expanduser()).absolute())
    return ProviderConfig(
        backend=backend,
        model=_require_str(raw, "model", "provider"),
        local_api_base=_require_str(raw, "local_api_base", "provider"),
        accounting_profile_path=profile,
    )


def _parse_observability(data: dict[str, object]) -> ObservabilityConfig:
    raw = _get_section(data, "observability")
    return ObservabilityConfig(
        log_level=_require_str(raw, "log_level", "observability"),
        log_format=_require_str(raw, "log_format", "observability"),
        log_path=_require_str(raw, "log_path", "observability"),
        metrics_enabled=_require_bool(raw, "metrics_enabled", "observability"),
    )


def _parse_templates(data: dict[str, object]) -> TemplatesConfig:
    raw = _get_section(data, "templates")
    return TemplatesConfig(
        version=_require_str(raw, "version", "templates"),
        base_path=_require_str(raw, "base_path", "templates"),
    )


def load_config(path: Path | None = None) -> ApprenticeConfig:
    """Load and validate config from a TOML file.

    Defaults to config/apprentice.toml relative to the project root. Numbers
    are exact (TOML floats parse as Decimal). Only pure parsing happens here:
    nothing is created on disk.
    Raises ValueError on missing required fields, removed keys or backends and
    out-of-range limits, TypeError on wrong types, KeyError on unresolved
    required environment variables.
    """
    config_path = path if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("rb") as f:
        raw: dict[str, object] = tomllib.load(f, parse_float=Decimal)

    data = _interpolate_dict(raw)
    _reject_removed_keys(data)

    return ApprenticeConfig(
        budget=_parse_budget(data),
        rate_limits=_parse_rate_limits(data),
        agents=_parse_agents(data),
        circuit_breaker=_parse_circuit_breaker(data),
        provider=_parse_provider(data, config_path.absolute().parent),
        observability=_parse_observability(data),
        templates=_parse_templates(data),
    )
