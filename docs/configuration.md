# Configuration Reference

Configuration lives in `config/apprentice.toml`. Environment variables are interpolated using `${VAR}` or `${VAR:-default}` syntax. Numbers are exact: TOML floats are read as decimals, USD amounts must be whole nanodollars (at most nine decimal places), and booleans are never accepted as numbers. Every section below is required.

## [provider]

```toml
[provider]
backend = "openai"                 # openai or local
model = "openai/gpt-5.4"           # the profile's requested_model, optionally prefixed "openai/"
local_api_base = ""                # loopback URL of a non-hosted server (local only)
accounting_profile_path = ""       # path to the route's accounting profile; "" denies every call
```

A relative `accounting_profile_path` resolves against the directory of the config file. The route (backend, model, profile, pinned SDKs and price data) is resolved before any work is admitted; anything that does not qualify is reported and nothing is sent. `--backend`/`--model` overrides on `build`, `retry` and `suggest` go through the same check.

| Backend | Endpoint | Credential | Profile kind |
|---|---|---|---|
| `openai` | exactly `https://api.openai.com/v1`, standard tier, `store=false` | `OPENAI_API_KEY` | `openai-standard-responses` |
| `local` | `local_api_base`: a loopback IP address, no credentials/query/fragment | none sent | `non-hosted-responses` |

The client uses no SDK or HTTP retries, ignores environment proxies and does not follow redirects. An `OPENAI_BASE_URL` or `OPENAI_API_BASE` environment variable that names a different endpoint is rejected rather than honoured.

Removed backends — `anthropic`, `gemini`, `ollama` and `claude_cli` — are rejected with their reason: none has a qualified metered profile. A plain OpenAI-compatible or Ollama chat server is not a `local` route by itself; it qualifies only if it implements the counted Responses protocol described below.

## Accounting profile

One version-1 JSON profile qualifies the configured route. Software checks its structure (known kind, exact model, operations and tier, finite non-negative fee bounds in whole nanodollars, provenance digest/source/date and the operator's attestation). It does not and cannot check that the declared fees and capabilities are genuine; that is the operator's obligation. The qualification date is provenance only — there is no invented expiry. apprentice ships no profile and no default prices.

### Paid route (`openai-standard-responses`)

```json
{
  "version": 1,
  "kind": "openai-standard-responses",
  "provider": "openai",
  "requested_model": "gpt-5.4",
  "response_models": ["gpt-5.4-2026-03-05"],
  "service_tier": "default",
  "operations": {
    "responses.input_tokens": {"fee_upper_bound_usd": "<qualified fee bound>", "max_request_bytes": null},
    "responses.create": {"rates": "pinned-sdk-price-data"}
  },
  "capabilities": {"max_input_tokens": 1050000, "max_output_tokens": 128000,
                   "cached_input_subset": true, "reasoning_output_subset": true},
  "provenance": {"source": "<where the fee and capabilities were qualified>",
                 "sha256": "<digest of that source>", "qualified_on": "YYYY-MM-DD"},
  "operator_attestation": {"attested_by": "<operator>", "statement": "<attestation>"}
}
```

- Generation rates come only from the pinned `litellm` price data (`model_prices_and_context_window_backup.json`, SHA-256 `7aacdb30…`), standard tier, including cached-input rates and the switch to the higher rates for the whole request once input exceeds 272,000 tokens. Every `response_models` entry must be priced exactly like `requested_model`; the echoed model must be one of them.
- The counting endpoint is not assumed free: without a profile there is no fee bound, so nothing is counted or generated. Its `input_tokens` result measures request size; it is not billed usage. `max_request_bytes` (or `null` for any size) is the request envelope the fee bound covers.
- `capabilities` may only narrow the pinned SDK model record, never widen it.

### Non-hosted route (`non-hosted-responses`)

```json
{
  "version": 1,
  "kind": "non-hosted-responses",
  "requested_model": "<the server's own model ID>",
  "protocol": "counted-responses",
  "capabilities": {"context_window_tokens": 8192, "max_output_tokens": 1024,
                   "cached_input_subset": false, "reasoning_output_subset": false},
  "cost_policy": "zero-hosted",
  "provenance": {"source": "...", "sha256": "...", "qualified_on": "YYYY-MM-DD"},
  "operator_declaration": {"genuinely_non_hosted": true, "attested_by": "<operator>", "statement": "..."}
}
```

The server must answer `POST /responses/input_tokens` and `POST /responses` for the same complete input, echo the requested model and report complete usage (`input_tokens`, `output_tokens`, `total_tokens` and the subset details the profile declares). The declared context and output maxima are the model's own; nothing is inherited from a GPT model.

- `cost_policy = "zero-hosted"`: actual hosted cost is known to be zero; every token is still reserved and settled against the token ceilings.
- `cost_policy = "sdk-reference-capacity"` plus `"reference_rate_model": "<priced model>"`: USD ceilings are charged at that reference model's pinned rates as **modelled capacity** (including its tier switch, applied to the local counted units). Reports keep it apart from hosted spend; it is never an invoice. The reference supplies rates only — never identity, tokenizer or context.

## Budget enforcement

Each call is admitted against every scope at once, in one transaction of the installation ledger, before it is sent:

| Key | Scope |
|---|---|
| `budget.global.monthly_token_ceiling`, `monthly_cost_ceiling_usd` | UTC calendar month fixed when a call's attempt is admitted, shared by every process, retry and command |
| `budget.cycle.max_tokens_per_cycle`, `max_cost_per_cycle_usd` | one controlled cycle (`build`, `retry`, `suggest`, library build); a retry is a new cycle |
| `budget.stage.max_tokens_per_stage` | one stage of a cycle (implementation, artifact generation, discovery) across its parallel roles and all iterations |
| `budget.agent.max_tokens_per_agent_call` | one call: counted input plus transported output cap |
| `budget.agent.implementation_budget_pct` | the drafter's share of the cycle's token and USD ceilings |
| `budget.agent.tool_agent_budget_pct` | each of instrumentation, visualization and assessment, separately |
| `agents.max_implementation_retries` | total drafter attempts per cycle, including the first |

- Tokens are complete input plus all output; cached input and reasoning tokens are subsets and are not counted twice. USD amounts are the declared policy quote in whole nanodollars, not a provider invoice and not other clients' spend.
- Implementation 40% plus three 15% tool shares allocate 85% of a cycle; the rest is deliberately unallocated (artifact review is programmatic and makes no model call). Discovery uses only its stage and cycle ceilings. Shares are ceilings, not entitlements.
- The paid counting fee bound is reserved before counting and stays charged even if generation is then denied. Generation reserves counted input plus the largest output cap that every scope admits, with the worst-case quote (all input uncached). The cap adapts to current headroom including other calls in flight, so parallel roles may get different caps; the cap is recorded with the call. An explicit request cap is never raised.
- A call that does not fit waits only while other in-flight calls (which may still release unused allowance) are the sole obstacle; committed usage, unknown holds and exhausted ceilings deny it.
- Usage is settled once from the raw response before any SDK conversion; unused allowance is refunded. Ctrl-C or `SIGTERM` to the CLI cancels the cycle (recorded `cancelled`); a `SIGTERM` that arrives while the cycle's admission or its run record and terminal state are being committed waits for them, so the run and cycle keep the outcome they reached (for example `completed`) and the command then exits 143. The termination JSON reports the outcome the command's cycle committed with its `cycle_id` (and `run_id` for a build or retry), and no outcome when the stop arrived before any cycle ended. If the run record cannot be written on the way out, the cycle is still recorded with the outcome it reached — `cancelled` (neutral, not owner loss), or `failed` when the stop arrived while a failed run's end was being recorded — the command still exits 143 and the termination `error` names the unsaved record; the record keeps its previous state. A killed process is recovered as owner loss. After a request is sent, any failure, cancellation, crash or missing usage keeps the full reservation held as unknown, never zero. Inconsistent or above-bound usage is charged at no less than the reservation and quarantines the profile. Streaming requests are refused before anything is reserved.
- Ceilings and percentages may be zero to deny their scope; `max_implementation_retries` must be at least 1.

The ledger lives under the run-record store root, in `controls/` (`authority.id`, `accounting.sqlite3`, `leases/<slot>.lock`). A missing, partial, foreign, corrupt or unsupported ledger, or a clock that runs backwards, stops all admission; it is never recreated or reset. A process that dies keeps its in-flight reservations held as unknown. `apprentice status` reports configured limits separately from ledger state.

### Earlier installations

On a fresh installation — none of the paths below exists — the first command whose configuration loads (including `config`, `history` or a refused build) creates the ledger from that empty footprint before it writes its own logs, so its own files are never taken for earlier use. If earlier apprentice state already existed (the store root, the configured log root, or `~/.apprentice/sessions` / `~/.apprentice/logs`), past `suggest`/library use may have left no record, so the current month is held as unknown and no model call is admitted until the next UTC month; no actual debit is invented. Run records that may still be in progress block every cycle until the operator runs `apprentice controls adopt-legacy --operator <name> --declare-no-earlier-process-running`. Every admission also rescans the store: a run record written without the authority (for example by an older binary) holds its month and the current month as unknown. Older or foreign binaries can bypass these application guards; the ledger covers this installation only, not the provider account.

## [agents]

```toml
[agents]
max_implementation_retries = 3    # total drafter attempts per cycle, including the first
```

After every draft the validators run; a passing draft ends the loop, a failing one feeds its issues into the next attempt. When all attempts fail, the blocking gates fail the run.

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

Validated (non-negative counts; at least one concurrent item; `cooldown_hours` small enough that a deadline that far from now is representable, i.e. before the year 9999) but not yet enforced.

## [circuit_breaker]

```toml
[circuit_breaker]
failure_threshold = 3
half_open_probe_after_minutes = 60
max_open_cycles_before_manual_reset = 3
```

Validated (threshold and maximum at least 1; `half_open_probe_after_minutes` small enough that a deadline that far from now is representable) but not yet enforced. A ledger whose stored effective policy breaks these bounds is reported as `control authority unavailable` and kept as it is.

## [observability]

```toml
[observability]
log_level = "INFO"
log_format = "json"
log_path = "${HOME}/.apprentice/logs"
metrics_enabled = true
```

## [templates]

```toml
[templates]
version = "1.0.0"
base_path = "config/templates"
```

## Removed settings

These keys were accepted earlier but never enforced; they are now rejected with their migration instead of being ignored:

| Key | Migration |
|---|---|
| `provider.fallback_model` | delete — apprentice never switches models |
| `budget.cycle.max_algorithms_per_cycle` | delete — one cycle builds one algorithm |
| `budget.agent.review_budget_pct` | delete — review is programmatic, no model call |
| `agents.max_review_rounds` | delete — review runs once |
| `agents.max_tool_agent_retries` | delete — tool agents run once per cycle |
| `[gates]` (`max_lint_retries`, `max_correctness_retries`, `max_review_rounds`) | delete the section — gates run once |
| `observability.alert_on_circuit_open`, `alert_webhook` | delete — there is no alert transport |

## Convention Schema

`config/no-magic-schema.yaml` defines artifact naming conventions:

- File naming: `micro{snake_case_name}.py` prefix
- Tier directories: `01-foundations`, `02-alignment`, `03-systems`, `04-agents`
- Required docstring sections: summary, args, returns, complexity, references
- Instrumentation trace keys: step, operation, state
- Anki card types: concept, complexity, implementation, comparison
