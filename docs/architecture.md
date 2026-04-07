# Architecture

## Agent Pipeline

apprentice uses Google ADK to compose agents into a sequential pipeline with parallel fan-out:

```
SequentialAgent("apprentice_pipeline")
├── LoopAgent("implementation_loop")
│   ├── LlmAgent("drafter")
│   └── LlmAgent("self_reviewer")
├── ParallelAgent("artifact_generation")
│   ├── LlmAgent("instrumentation")
│   ├── LlmAgent("visualization")
│   └── LlmAgent("assessment")
├── LoopAgent("review_loop")
│   └── LlmAgent("reviewer")
└── LlmAgent("packaging")            # only on `submit`
```

### Implementation Loop

The drafter generates algorithm code. The self_reviewer validates it using FunctionTool wrappers around the existing validators (lint, correctness, stdlib check). On failure, the reviewer summarizes issues for the drafter to fix. The loop exits when all validators pass or `max_iterations` (default 3) is reached.

### Artifact Generation

Three agents run concurrently:
- **Instrumentation** reads the implementation from session state and adds trace hooks
- **Visualization** generates a Manim animation scene (optionally using a scaffold template)
- **Assessment** generates Anki flashcards in CSV format

### Review Loop

The reviewer runs consistency and schema compliance validators across all artifacts. Exits on pass or after `max_iterations` (default 2) rounds.

### Packaging

Only runs on `apprentice submit`. Creates coordinated PRs in both `no-magic` and `no-magic-viz` repos with proper file placement and cross-references.

## Session State

ADK agents communicate through session state. Each agent writes to a key specified by `output_key`:

| Agent | output_key | Content |
|---|---|---|
| drafter | `generated_code` | Python source code |
| self_reviewer | `review_feedback` | Validation issues or "passed" |
| instrumentation | `instrumented_code` | Python source with trace hooks |
| visualization | `manim_scene_code` | Manim Scene class |
| assessment | `anki_deck_content` | CSV flashcard content |
| reviewer | `review_verdict` | Pass/fail with details |
| packaging | `pr_urls` | Dict of PR URLs |
| discovery | `discovery_candidates` | JSON array of candidates |

Agents read from other agents' keys using `{key_name}` in their instruction templates.

## Budget System

`BudgetTracker` in `core/budget.py` tracks tokens and cost per agent:

- `before_agent_callback` — records start time, logs dispatch
- `after_agent_callback` — records completion, accumulates tokens/cost
- `before_model_callback` / `after_model_callback` — log LLM request/response

Budget is configured in `apprentice.toml` under `[budget]`:
- Global: monthly token/cost ceiling
- Cycle: per-pipeline-run limits
- Agent: percentage allocation (implementation 40%, tool agents 15% each, review 15%)

## Session Persistence

`SessionStore` in `core/session_store.py` persists run records as JSON files in `~/.apprentice/sessions/`. Each record captures:

- Session state (all agent outputs)
- Budget summary (per-agent token/cost breakdown)
- Timing, status, and error information

This enables:
- `apprentice retry <run-id>` — rerun failed pipelines
- `apprentice history` — list past runs
- `apprentice metrics` — aggregate success rates and costs

## Provider Abstraction

`LiteLlm` from ADK provides a unified interface across providers. The factory in `providers/factory.py` handles:

- Environment variable setup per backend
- API key validation for cloud providers
- Base URL configuration for local providers (Ollama, OpenAI-compatible)

All agents share the same model instance. Override at runtime with `--backend` and `--model` CLI flags.
