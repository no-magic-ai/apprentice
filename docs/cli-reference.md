# CLI Reference

## Global Options

```
apprentice [--version] [--config PATH] <command>
```

- `--version` — print version and exit
- `--config PATH` — path to `apprentice.toml` (default: `config/apprentice.toml`)

## Commands

### build

Run the pipeline through review (no packaging).

```
apprentice build <algorithm> [--tier N] [--description TEXT] [--backend NAME] [--model STRING]
```

- `algorithm` — algorithm name (e.g. "quicksort")
- `--tier` — algorithm tier 1-4 (default: 2)
- `--description` — optional description for the LLM
- `--backend` — override provider backend (anthropic, openai, gemini, ollama, local)
- `--model` — override LiteLLM model string (e.g. "ollama_chat/llama3.3")

Persists run state to `~/.apprentice/sessions/` for retry support.

### submit

Run the full pipeline including packaging (PR creation).

```
apprentice submit <algorithm> [--tier N] [--backend NAME] [--model STRING]
```

Same options as `build`. Creates PRs in both `no-magic` and `no-magic-viz` repos.

### suggest

Run the discovery agent to suggest candidate algorithms.

```
apprentice suggest [--tier N] [--limit N] [--backend NAME] [--model STRING]
```

- `--tier` — target tier (default: 2)
- `--limit` — max candidates to suggest (default: 5)

### retry

Retry a failed pipeline run.

```
apprentice retry <run_id> [--backend NAME] [--model STRING]
```

- `run_id` — ID from `apprentice history` output

Reruns the full pipeline for the same algorithm and tier.

### history

List past pipeline runs.

```
apprentice history [--status STATUS] [--limit N]
```

- `--status` — filter by status: completed, failed, in_progress
- `--limit` — max entries (default: 20)

### metrics

Show aggregated metrics across all recorded runs.

```
apprentice metrics
```

Reports success rate, per-agent cost/token breakdown, and per-tier statistics.

### preview

Inspect artifacts from the last build.

```
apprentice preview
```

### status

Show budget usage and system state.

```
apprentice status
```

### config

Display current configuration.

```
apprentice config
```

### dev

Launch ADK dev UI for interactive debugging.

```
apprentice dev [--port N]
```

- `--port` — dev UI port (default: 8080)
