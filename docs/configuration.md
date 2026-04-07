# Configuration Reference

Configuration lives in `config/apprentice.toml`. Environment variables are interpolated using `${VAR}` or `${VAR:-default}` syntax.

## [provider]

```toml
[provider]
backend = "anthropic"                            # Provider backend
model = "anthropic/claude-sonnet-4-20250514"     # LiteLLM model string
fallback_model = "anthropic/claude-haiku-4-5-20251001"
local_api_base = ""                              # API base URL for ollama/local
```

### Supported backends

| Backend | Model string format | Required env var |
|---|---|---|
| `anthropic` | `anthropic/claude-sonnet-4-20250514` | `ANTHROPIC_API_KEY` |
| `openai` | `openai/gpt-4.1` | `OPENAI_API_KEY` |
| `gemini` | `gemini/gemini-2.5-flash` | `GOOGLE_API_KEY` |
| `ollama` | `ollama_chat/llama3.3` | none |
| `local` | `openai/local-model` | none |

For `ollama`: set `local_api_base` to the Ollama API URL (default `http://localhost:11434`).
For `local`: set `local_api_base` to any OpenAI-compatible API URL.

## [budget]

### [budget.global]

```toml
[budget.global]
monthly_token_ceiling = 2_000_000
monthly_cost_ceiling_usd = 50.0
```

### [budget.cycle]

Per-pipeline-run limits:

```toml
[budget.cycle]
max_tokens_per_cycle = 100_000
max_cost_per_cycle_usd = 5.0
max_algorithms_per_cycle = 3
```

### [budget.stage]

```toml
[budget.stage]
max_tokens_per_stage = 20_000
```

### [budget.agent]

Per-agent budget allocation as percentage of cycle budget:

```toml
[budget.agent]
max_tokens_per_agent_call = 20_000
implementation_budget_pct = 40
tool_agent_budget_pct = 15
review_budget_pct = 15
```

## [agents]

```toml
[agents]
max_implementation_retries = 3    # LoopAgent max_iterations for implementation
max_review_rounds = 2             # LoopAgent max_iterations for review
max_tool_agent_retries = 1        # Retry limit for tool agents
```

## [rate_limits]

```toml
[rate_limits]
max_prs_per_day = 2
max_prs_per_week = 5
max_concurrent_items = 1
cooldown_hours = 4
max_files_per_pr = 10
max_lines_per_pr = 2000
```

## [gates]

```toml
[gates]
max_lint_retries = 2
max_correctness_retries = 1
max_review_rounds = 2
```

## [circuit_breaker]

```toml
[circuit_breaker]
failure_threshold = 3
half_open_probe_after_minutes = 60
max_open_cycles_before_manual_reset = 3
```

## [observability]

```toml
[observability]
log_level = "INFO"
log_format = "json"
log_path = "${HOME}/.apprentice/logs"
metrics_enabled = true
alert_on_circuit_open = true
alert_webhook = ""
```

## [templates]

```toml
[templates]
version = "1.0.0"
base_path = "config/templates"
```

## Convention Schema

`config/no-magic-schema.yaml` defines artifact naming conventions:

- File naming: `micro{snake_case_name}.py` prefix
- Tier directories: `01-foundations`, `02-alignment`, `03-systems`, `04-agents`
- Required docstring sections: summary, args, returns, complexity, references
- Instrumentation trace keys: step, operation, state
- Anki card types: concept, complexity, implementation, comparison
