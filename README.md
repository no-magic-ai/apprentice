# apprentice

Agentic Algorithm Factory for [no-magic](https://github.com/no-magic-ai/no-magic) — automates algorithm entry creation with containment guardrails.

## What it does

`apprentice` generates complete algorithm entries for the no-magic educational catalog:

- **Implementation** — single-file, zero-dependency Python with type hints and tests
- **Instrumentation** — step-by-step trace hooks for learner replay
- **Visualization** — Manim animation scene from scaffold templates
- **Assessment** — Anki flashcard deck (concept, complexity, implementation, comparison)

Every artifact passes through quality gates (lint, correctness, consistency, schema compliance) before a human reviews and merges.

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

Session state flows data between agents via `output_key`. Budget tracked per-agent via ADK callbacks.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env  # Add your API keys
```

### Provider Configuration

Edit `config/apprentice.toml`:

```toml
[provider]
backend = "anthropic"                            # anthropic, openai, gemini, ollama, local
model = "anthropic/claude-sonnet-4-20250514"     # LiteLLM model string
fallback_model = "anthropic/claude-haiku-4-5-20251001"
local_api_base = ""                              # For ollama/local backends
```

Required environment variables per backend:
- `anthropic` → `ANTHROPIC_API_KEY`
- `openai` → `OPENAI_API_KEY`
- `gemini` → `GOOGLE_API_KEY`
- `ollama` → none (uses `local_api_base`)
- `local` → none (uses `local_api_base`, sets `OPENAI_API_KEY=not-needed`)

## Usage

```bash
# Build all artifacts for an algorithm
apprentice build "quicksort" --tier 2

# Build with a different provider
apprentice build "quicksort" --backend ollama --model ollama_chat/llama3.3

# Submit artifacts as PRs to no-magic repos
apprentice submit "quicksort" --tier 2

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
