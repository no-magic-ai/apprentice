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
└── LoopAgent("review_loop")
    └── LlmAgent("reviewer")
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

### Gate Verdicts

Each `GateAgent` computes one verdict entry (gate, stage, verdict, whether the gate blocks, diagnostics). It records the entry in the pipeline's `BudgetTracker` and yields it as an `EventActions.state_delta` appended to `gate_verdicts`, so the session service stores it. PASS, WARN and a FAIL from a non-blocking gate continue. A FAIL from a blocking gate, including a gate that raised, then raises `BlockingGateError`; the error propagates through `SequentialAgent` and the Runner, so no later agent runs. (ADK copies each child's invocation context, so setting a flag on it would not stop the parent.)

The CLI runner catches only that error, reads the stored session back from the same session service and attaches it. `build`, `retry` and `scripts/integration_test.py` then record the run once as `failed` with that state, the tracker's budget summary, the elapsed time and an error naming the gate; the work-root files stay and nothing is sealed, so the run cannot be approved or submitted. Other exceptions are recorded as before. With an empty draft the correctness gate fails first, so such a run also ends `failed` with the gate's diagnostics.

The same recorded verdicts decide whether a run can be sealed and published. `complete_run` refuses to seal a budget summary that holds a blocking FAIL, and `approve` and `submit` refuse a completed run whose `budget_summary.gate_verdicts` include one (runs sealed before the halt existed). Verdicts recorded without the `blocking` flag come from the four pipeline gates, which all block; WARN and non-blocking FAIL verdicts do not.

`wire_agent_callbacks` replaces `before_agent_callback`/`after_agent_callback` on every agent of the assembled pipeline, so the drafter's validation callback, the implementation loop's exit check and the review loop's validation callback do not run there. Those callbacks are exercised directly in tests.

### Packaging

Packaging is not an agent and is not part of the pipeline. `apprentice submit <algorithm> --run-id <run-id>` runs no model, generation graph, drafting or rendering:

1. The human-review gate (`gates/review.py`) requires a completed run, loads the run's sealed bundle once and verifies it (see [Run-Owned Artifacts](#run-owned-artifacts)), refuses a run whose build recorded a failed blocking gate, then requires an approval recorded by `apprentice approve <run-id>` and requires the approval, run record, bundle manifest and requested algorithm (and tier, if given) to agree. A run without a sealed bundle gets the rebuild instruction, not an approve remediation. Any mismatch, tampering, added or removed file, symlink or changed destination fails here, before any clone or other packaging side effect.
2. `agents/packaging.py` writes exactly those captured bytes to their manifest destinations (`no-magic/<tier dir>/micro<name>.py`, `no-magic-viz/scenes/scene_micro<name>.py`) in fresh clones of the two fixed repositories inside an exclusive scratch root, refusing existing destinations and symlinked directories. It stages only those paths, commits with the approval time as author and committer date, and checks that each commit contains exactly the approved bytes and paths before anything is pushed.
3. It pushes branch `apprentice/<run-id>` to both repositories and opens the PRs with `gh`, the viz PR referencing the core PR.

Each run gets one submission attempt, recorded on the run record. `approve` and `submit` serialize on a run with an exclusive advisory lock (`flock`) on `runs/<run-id>.lock`, an empty file that is created once and never replaced or removed. The lock is held only for short local steps (reading, checking and saving the record), never while cloning, pushing or calling `gh`. Under it, `submit` reloads the record, runs the review gate above (capturing the bundle bytes once), refuses any recorded attempt, allocates a fresh scratch root and saves the attempt as `pending` with the manifest digest, scratch workspace and branch. It then releases the lock and clones, pushes and opens PRs using only the captured bytes and the approval read under the lock. Afterwards it takes the lock again and, only if the stored attempt is still exactly the one it reserved, saves it as `complete` (both branches pushed, both PRs opened), `partial` (some branch was pushed; the record lists which branches were pushed and which PRs were opened before the error) or `failed` (nothing was pushed), with the error. If the record was deleted or its attempt changed meanwhile, nothing is saved and `submit` exits non-zero, printing the effects it knows about and the discrepancy.

A run with any recorded attempt, including one left `pending`, is refused without touching a repository, and the stored attempt is printed as recorded: `submit` never retries, resumes or reconciles publication. The operator inspects the recorded effects. A process killed before the `pending` save leaves at most an empty scratch root, and the run can be submitted again; one killed after it leaves the attempt `pending` with its remote effects unknown. These guarantees cover a process being killed and restarted on one machine. Record writes are atomic renames without `fsync`, so nothing is claimed about power loss, and the lock does not coordinate processes on different machines.

`approve` takes the same lock and is refused once the run has an attempt, so the approval an attempt publishes cannot change; re-approving before any attempt replaces the approval. The approver (`--approver`, otherwise `$GITHUB_USER`, then `$USER`; an explicit value never falls back) must be a non-blank string without CR, LF or NUL and is stored exactly as given. The review gate refuses a stored approval whose approver breaks that rule or whose `approved_at` is not a canonical timezone-aware ISO 8601 timestamp, before any attempt is recorded.

Instrumented code and Anki cards are kept in the approved bundle but not promoted.

Packaging does not yet produce what released no-magic v3 requires beyond those two repos: a `no-magic-papers` card whose `implementations[]` references the script, and an explicit `SCRIPT_TO_PAPER` entry in `no-magic/scripts/generate_catalog.py`. The M1 catalog extension to that generator defines an additional per-script `SCRIPT_CONTRACTS` record (teaching kind, data source, adaptation note) that released v3 does not contain; when the target no-magic generator revision defines `SCRIPT_CONTRACTS`, packaging must populate it as well.

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

Only the cycle token/cost limits are consumed: the pipeline's shared `BudgetTracker` is created from them (`core/orchestrator.py`). When the tracker is exhausted, `before_agent_budget_check` logs a warning and still dispatches the agent, so the limit is observed, not enforced. The monthly, per-stage, per-agent-call and percentage-allocation settings, `[rate_limits]` and `[circuit_breaker]` are parsed and shown by `apprentice config` / `status` but not enforced. The enforced cap is the hard-coded ADK `RunConfig(max_llm_calls=...)` per run in `cli.py`.

## Session Persistence

`SessionStore` in `core/session_store.py` persists run records as JSON files in `~/.apprentice/sessions/`. Each record captures:

- Session state (all agent outputs)
- Budget summary (per-agent token/cost breakdown)
- Timing, status, and error information
- The sealed bundle's manifest digest and, once approved, the approval

Run IDs are `<algorithm>-<UTC second>-<uuid>`, created exclusively, so two runs of the same algorithm never share a record or files. Lookups use the exact ID (older `<algorithm>-<UTC second>` IDs still load); `history` orders runs by their recorded `started_at`. A stored record must be a JSON object whose `run_id`, `algorithm_name`, `status` and `started_at` are strings and whose `tier` is an integer (not a boolean or float); other fields are kept as stored. Loading or listing a record that breaks this, or listing one whose `started_at` is not an ISO 8601 timestamp, fails with an error naming the file: `history`, `metrics`, `preview` without `--run-id`, `submit` without `--run-id` and `scripts/integration_test.py --report-only` report that error and exit 1 rather than skipping the record.

## Run-Owned Artifacts

`SessionStore` is the only allocator of artifact roots:

```
~/.apprentice/sessions/runs/<run-id>/work/     mutable files the gates and validators check
~/.apprentice/sessions/runs/<run-id>/bundle/   sealed bundle written once at completion
~/.apprentice/sessions/scratch/<uuid>/         exclusive roots for work without a run record
```

Every writer (`GateAgent`, the drafter and review callbacks — which do not run in the assembled pipeline, see [Gate Verdicts](#gate-verdicts) — and the legacy `stages/*`) uses the shared fixed role filenames in `core/artifacts.py` (`implementation.py`, `instrumented.py`, `scene.py`, `cards.csv`, `validation_report.json`, `discovery.json`) and refuses a missing or symlinked root and symlinked or multiply-linked files. No path is derived from the algorithm name, the current directory or a temporary directory.

Completing a run seals its final artifacts into `bundle/` with a `manifest.json`: canonical UTF-8 JSON (sorted keys, fixed separators) with the run ID, algorithm, integer tier and, per artifact, its role, file name, size, SHA-256 and repository destination (`null` when the role is kept but not promoted). The manifest digest is the SHA-256 of that JSON without its own `manifest_sha256` field. `preview`, `approve` and `submit` reload the bundle and reject a malformed or non-canonical manifest, changed identity or destinations, added or removed files, symlinks and any byte change. A run recorded before sealed bundles existed has no bundle; it fails with an instruction to rebuild rather than being regenerated.

The legacy `core/pipeline.Pipeline` takes the same isolation through `PipelineContext.artifact_root`: one fresh `SessionStore.allocate_work_root()` per invocation. `Pipeline.run` refuses an unset, missing, symlinked or already-populated root before any stage runs, and each stage refuses a missing root before calling its provider. This is storage isolation only; it adds no execution containment, budget enforcement or approval record.

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
