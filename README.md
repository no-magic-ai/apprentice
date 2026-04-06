# apprentice

Agentic Algorithm Factory for [no-magic](https://github.com/no-magic-ai/no-magic) — automates algorithm entry creation with containment guardrails.

## What it does

`apprentice` generates complete algorithm entries for the no-magic educational catalog:

- **Implementation** — single-file, zero-dependency Python with type hints and tests
- **Instrumentation** — step-by-step trace hooks for learner replay
- **Visualization** — Manim animation scene from scaffold templates
- **Assessment** — Anki flashcard deck (concept, complexity, implementation, comparison)
- **Documentation** — README section with usage and complexity analysis

Every artifact passes through quality gates (lint, correctness, consistency, schema compliance) before a human reviews and merges.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

```bash
# Suggest algorithms for a tier
apprentice suggest --tier 2 --limit 5

# Build all artifacts for an algorithm
apprentice build "quickselect"

# Preview generated artifacts
apprentice preview

# Submit as PR
apprentice submit

# Check budget and queue state
apprentice status
```

## Architecture

See [`apprentice-system-design.md`](../apprentice-system-design.md) for the full system design and [`apprentice-development-plan.md`](../apprentice-development-plan.md) for the phased implementation plan.

## License

MIT
