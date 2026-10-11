# apprentice

Agentic Algorithm Factory for [no-magic](https://github.com/no-magic-ai/no-magic) — a maintainer-side tool that drafts algorithm entries for human review.

## Status

`apprentice` is implemented software: package version 0.4.0 with a Google ADK pipeline and the CLI below. It is **not activated** for autonomous operation. The umbrella strategy keeps activation deferred until the no-magic catalog reaches at least 60 algorithms and the per-algorithm template is locked (48 catalog scripts today). Meeting that threshold would not be enough on its own: live use also needs separate safety, spend and human approvals, and the following gaps are open in the current code:

- **Budgets and limits.** Every model call is reserved before it is sent and settled once from the provider's raw usage in one durable installation ledger: monthly, cycle, stage, role-percentage and per-call token and USD ceilings are enforced across processes and restarts (see [Configuration](docs/configuration.md#budget-enforcement)). A paid route needs an operator-supplied accounting profile; apprentice ships none, so it denies paid calls until one is configured. Concurrent items, cooldown, rolling PR day/week windows, per-PR file and text-line limits and the automated-work circuit are enforced at cycle admission and before a submission's first remote write. These controls cover this installation only; older or foreign binaries can bypass them. `core/queue.py` and `core/scheduler.py` are empty modules.
- **Paper-aware packaging.** Packaging targets `no-magic` and `no-magic-viz` only. Released no-magic v3 also requires a `no-magic-papers` card whose `implementations[]` references the script and an explicit `SCRIPT_TO_PAPER` entry in `no-magic/scripts/generate_catalog.py`; packaging produces neither yet. The M1 catalog extension to that generator defines an additional per-script `SCRIPT_CONTRACTS` record (teaching kind, data source, adaptation note) that released v3 does not contain; when the target no-magic generator revision defines `SCRIPT_CONTRACTS`, packaging must populate it as well.

Approved-byte submission is in place: each run writes its artifacts under its own run-owned root and, on completion, seals them into an immutable bundle whose manifest binds the run identity, every artifact hash and each repository destination. `approve` binds a human approval to that manifest, and `submit` re-verifies the bundle and pushes exactly those bytes to their destinations without calling a model or regenerating anything. A run recorded before sealed bundles existed fails with a rebuild instruction. The approval is a local operator attestation, not a cryptographic identity.

Generated code never runs on the host. Every implementation draft, the correctness gate and a render of the generated Manim scene run in one explicitly configured local Docker sandbox: no network, read-only root, no capabilities, bounded CPU, memory, processes, wall time and output, nothing from the host mounted, and every container removed (and its absence confirmed) before a result counts. Without a configured backend and an operator-built sandbox image, builds are refused before any admission or model request; there is no fallback. See [Configuration](docs/configuration.md#execution) for the image build and the exact limits; this is container isolation, not a certified sandbox.

Publication credentials are separate from generation. `submit` publishes only with the token given explicitly in `APPRENTICE_PUBLISH_TOKEN`, validated before anything is admitted and handed only to the `git push` and `gh pr create` subprocesses, which run with a private, empty home and no ambient git, gh or credential-helper configuration; `gh` logins, `GH_TOKEN` and `GITHUB_TOKEN` are never used. Generation and validation refuse to start while one of the known GitHub or apprentice publication token variables is set ([CLI Reference: submit](docs/cli-reference.md#submit)). Processes of the same host user are not isolated from each other.

`apprentice` is maintainer tooling, not a learner artifact. Its declared dependencies and hosted or local LLM providers are its own; the no-magic learner constraints (single file, standard library, CPU, no services) apply to the scripts it drafts, not to `apprentice` itself.

## What it does

`apprentice` generates complete algorithm entries for the no-magic educational catalog:

- **Implementation** — single-file, zero-dependency Python with type hints and tests
- **Instrumentation** — step-by-step trace hooks for learner replay
- **Visualization** — Manim animation scene from scaffold templates
- **Assessment** — Anki flashcard deck (concept, complexity, implementation, comparison)

Every artifact passes through quality gates (lint, correctness, consistency, schema compliance, scene render) before a human reviews it. Nothing is merged by `apprentice`; a human reviews and merges every PR.

## Architecture

Built on [Google ADK](https://github.com/google/adk-python) with [LiteLLM](https://github.com/BerriAI/litellm) for multi-provider support.

```
SequentialAgent("apprentice_pipeline")
├── LoopAgent("implementation_loop", max=agents.max_implementation_retries)
│   ├── LlmAgent("drafter")                    → generates code; its callback runs
│   │                                            stdlib/lint validators and the draft
│   │                                            in the sandbox (correctness)
│   └── BaseAgent("implementation_checkpoint")  → ends the loop once a draft passed
├── gates: correctness (sandbox), lint (blocking)
├── ParallelAgent("artifact_generation")
│   ├── LlmAgent("instrumentation")  → adds trace hooks
│   ├── LlmAgent("visualization")    → generates Manim scene
│   └── LlmAgent("assessment")       → generates Anki cards
├── gates: consistency, schema compliance, scene render (sandbox) (blocking)
└── BaseAgent("reviewer")            → programmatic consistency + schema review, no model
```

`submit` does not run this pipeline: deterministic packaging promotes the approved bundle into PRs in no-magic + no-magic-viz.

Session state flows data between agents via `output_key`. Each role's model is metered at its client against the installation ledger; see [Status](#status) for what these controls do and do not cover.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and a local Docker daemon with the sandbox image built from `src/apprentice/sandbox/image/` ([Configuration](docs/configuration.md#execution)).

```bash
uv sync
export OPENAI_API_KEY=...  # the openai backend needs this and an accounting profile (below)
# build the sandbox image, then set [execution].docker_context and [execution].image
```

`google-adk`, `litellm` and `openai` are pinned exactly (1.28.1, 1.83.3, 2.30.0): generation prices come only from the price data inside the pinned `litellm`, verified by SHA-256, and a mismatch denies every model call.

### Provider Configuration

Edit `config/apprentice.toml`:

```toml
[provider]
backend = "openai"                 # openai or local
model = "openai/gpt-5.4"           # must be the profile's requested_model
local_api_base = ""                # loopback URL, local backend only
accounting_profile_path = ""       # operator-qualified profile; empty denies every call
```

- `openai` → `OPENAI_API_KEY`; requests go to exactly `https://api.openai.com/v1` at the standard tier with `store=false`. An `OPENAI_BASE_URL`/`OPENAI_API_BASE` pointing elsewhere is rejected.
- `local` → a genuinely non-hosted server on a loopback address that implements the counted Responses protocol for the profiled model; no credential is sent.

The `anthropic`, `gemini`, `ollama` and `claude_cli` backends and `provider.fallback_model` were removed: their usage and fees are not metered by a qualified profile. Configuring them is an error; apprentice never switches model or provider on its own. The CLI reads the process environment and does not load `.env` files. The accounting profile format is described in [Configuration](docs/configuration.md#accounting-profile).

## Usage

```bash
# Build all artifacts for an algorithm
apprentice build "quicksort" --tier 2

# Build with an overridden route (the accounting profile must qualify it)
apprentice build "quicksort" --backend local --model openai/<profiled-model>

# Record a human-review approval for a build run (required before submit)
apprentice approve <run-id>

# Open PRs in the no-magic repos with the exact approved bytes (no model call); the
# GitHub token is given to this command only, never to build/retry/suggest
APPRENTICE_PUBLISH_TOKEN=<token> apprentice submit "quicksort" --tier 2 --run-id <run-id>

# Suggest candidate algorithms for a tier
apprentice suggest --tier 2 --limit 5

# Retry a failed run
apprentice retry <run-id>

# View run history
apprentice history
apprentice history --status failed

# View aggregated metrics
apprentice metrics

# Preview a completed run's sealed artifact bundle (default: most recent completed run)
apprentice preview [--run-id <run-id>]

# Configured limits, ledger balances/holds, route validity and admission
apprentice status

# After upgrading over earlier state: declare no earlier apprentice process is running
apprentice controls adopt-legacy --operator <name> --declare-no-earlier-process-running

# Close a latched circuit after investigating; suspend continuity before a rollback
apprentice controls reset-circuit --operator <name>
apprentice controls prepare-rollback --operator <name>

# Display configuration
apprentice config
```

## Integration Testing

```bash
# Dry run — list algorithms without executing
uv run python scripts/integration_test.py --dry-run

# Run all tiers
uv run python scripts/integration_test.py

# Run specific tier with limit
uv run python scripts/integration_test.py --tier 2 --limit 3

# Run with a profiled local model
uv run python scripts/integration_test.py --backend local --model openai/<profiled-model>

# View report from past runs
uv run python scripts/integration_test.py --report-only
```

Reports saved to `~/.apprentice/reports/`.

## Local Model Setup

See [docs/local-models.md](docs/local-models.md) for the non-hosted counted-Responses requirements.

## Documentation

- [Architecture](docs/architecture.md) — agent design, session state flow, budget system
- [CLI Reference](docs/cli-reference.md) — all commands and flags
- [Configuration](docs/configuration.md) — apprentice.toml reference
- [Local Models](docs/local-models.md) — non-hosted route requirements
- [Troubleshooting](docs/troubleshooting.md) — common issues and solutions

## License

MIT
