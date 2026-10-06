# apprentice

Agentic Algorithm Factory for [no-magic](https://github.com/no-magic-ai/no-magic) — a maintainer-side tool that drafts algorithm entries for human review.

## Status

`apprentice` is implemented software: package version 0.4.0 with a Google ADK pipeline and the CLI below. It is **not activated** for autonomous operation. The umbrella strategy keeps activation deferred until the no-magic catalog reaches at least 60 algorithms and the per-algorithm template is locked (48 catalog scripts today). Meeting that threshold would not be enough on its own: live use also needs separate safety, spend and human approvals, and the following gaps are open in the current code:

- **Execution containment.** The correctness check runs generated code with `subprocess.run` and a 5-second timeout. That is a timeout, not a sandbox.
- **Approved-byte submission.** `submit` needs a recorded `approve`, and a review gate blocks it unless the artifact hashes match the approved ones. But `submit` re-runs the model pipeline to regenerate the artifacts instead of packaging the approved bytes, and artifacts are written to a shared temporary directory.
- **Budgets and limits.** Token and cost use are tracked per agent against the per-cycle budget, but exhaustion is only logged, not enforced; the enforced cap is a hard-coded ADK `max_llm_calls` limit per run. The monthly, per-stage, per-agent, rate-limit and circuit-breaker settings are parsed and displayed but not enforced; `core/circuit_breaker.py`, `core/queue.py` and `core/scheduler.py` are empty modules.
- **Paper-aware packaging.** Packaging targets `no-magic` and `no-magic-viz` only. Released no-magic v3 also requires a `no-magic-papers` card whose `implementations[]` references the script and an explicit `SCRIPT_TO_PAPER` entry in `no-magic/scripts/generate_catalog.py`; packaging produces neither yet. The M1 catalog extension to that generator defines an additional per-script `SCRIPT_CONTRACTS` record (teaching kind, data source, adaptation note) that released v3 does not contain; when the target no-magic generator revision defines `SCRIPT_CONTRACTS`, packaging must populate it as well.

`apprentice` is maintainer tooling, not a learner artifact. Its declared dependencies and hosted or local LLM providers are its own; the no-magic learner constraints (single file, standard library, CPU, no services) apply to the scripts it drafts, not to `apprentice` itself.

## What it does

`apprentice` generates complete algorithm entries for the no-magic educational catalog:

- **Implementation** — single-file, zero-dependency Python with type hints and tests
- **Instrumentation** — step-by-step trace hooks for learner replay
- **Visualization** — Manim animation scene from scaffold templates
- **Assessment** — Anki flashcard deck (concept, complexity, implementation, comparison)

Every artifact passes through quality gates (lint, correctness, consistency, schema compliance) before a human reviews it. Nothing is merged by `apprentice`; a human reviews and merges every PR.

## Architecture

Built on [Google ADK](https://github.com/google/adk-python) with [LiteLLM](https://github.com/BerriAI/litellm) for multi-provider support.

```
SequentialAgent("apprentice_pipeline")
├── LoopAgent("implementation_loop", max=3)
│   ├── LlmAgent("drafter")          → generates code
│   └── LlmAgent("self_reviewer")    → validates with lint/correctness/stdlib tools
├── ParallelAgent("artifact_generation")
│   ├── LlmAgent("instrumentation")  → adds trace hooks
│   ├── LlmAgent("visualization")    → generates Manim scene
│   └── LlmAgent("assessment")       → generates Anki cards
├── LoopAgent("review_loop", max=2)
│   └── LlmAgent("reviewer")         → consistency + schema validation
└── LlmAgent("packaging")            → creates PRs in no-magic + no-magic-viz
```

Session state flows data between agents via `output_key`. Token and cost use is tracked per agent via ADK callbacks; see [Status](#status) for what is not enforced.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
export OPENAI_API_KEY=...  # the default openai backend needs this; see below for other backends
```

### Provider Configuration

Edit `config/apprentice.toml`:

```toml
[provider]
backend = "openai"                 # anthropic, openai, gemini, ollama, local, claude_cli
model = "openai/gpt-5.4"           # LiteLLM model string
fallback_model = "openai/gpt-5.4-mini"
local_api_base = ""                # For ollama/local backends
```

Required environment variables per backend. Export them in the shell that runs `apprentice`; the CLI reads the process environment and does not load `.env` files:
- `anthropic` → `ANTHROPIC_API_KEY`
- `openai` → `OPENAI_API_KEY`
- `gemini` → `GOOGLE_API_KEY`
- `ollama` → none (uses `local_api_base`)
- `local` → none (uses `local_api_base`, sets `OPENAI_API_KEY=not-needed`)
- `claude_cli` → none (wraps a local `claude -p` subprocess)

## Usage

```bash
# Build all artifacts for an algorithm
apprentice build "quicksort" --tier 2

# Build with a different provider
apprentice build "quicksort" --backend ollama --model ollama_chat/llama3.3

# Record a human-review approval for a build run (required before submit)
apprentice approve <run-id>

# Submit artifacts as PRs to no-magic repos (re-runs the pipeline; see Status)
apprentice submit "quicksort" --tier 2 --run-id <run-id>

# Suggest candidate algorithms for a tier
apprentice suggest --tier 2 --limit 5

# Retry a failed run
apprentice retry <run-id>

# View run history
apprentice history
apprentice history --status failed

# View aggregated metrics
apprentice metrics

# Preview generated artifacts
apprentice preview

# Check budget and queue state
apprentice status

# Display configuration
apprentice config

# Launch ADK dev UI
apprentice dev
```

## Integration Testing

```bash
# Dry run — list algorithms without executing
uv run python scripts/integration_test.py --dry-run

# Run all tiers
uv run python scripts/integration_test.py

# Run specific tier with limit
uv run python scripts/integration_test.py --tier 2 --limit 3

# Run with local model
uv run python scripts/integration_test.py --backend ollama --model ollama_chat/llama3.3

# View report from past runs
uv run python scripts/integration_test.py --report-only
```

Reports saved to `~/.apprentice/reports/`.

## Local Model Setup

See [docs/local-models.md](docs/local-models.md) for Ollama and llama.cpp setup.

## Documentation

- [Architecture](docs/architecture.md) — agent design, session state flow, budget system
- [CLI Reference](docs/cli-reference.md) — all commands and flags
- [Configuration](docs/configuration.md) — apprentice.toml reference
- [Local Models](docs/local-models.md) — Ollama and llama.cpp setup
- [Troubleshooting](docs/troubleshooting.md) — common issues and solutions

## License

MIT
