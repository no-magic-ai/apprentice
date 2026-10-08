# Architecture

## Agent Pipeline

apprentice uses Google ADK to compose agents into a sequential pipeline with parallel fan-out:

```
SequentialAgent("apprentice_pipeline")
├── LoopAgent("implementation_loop")
│   ├── LlmAgent("drafter")
│   └── BaseAgent("implementation_checkpoint")
├── GateAgent(correctness), GateAgent(lint)
├── ParallelAgent("artifact_generation")
│   ├── LlmAgent("instrumentation")
│   ├── LlmAgent("visualization")
│   └── LlmAgent("assessment")
├── GateAgent(consistency), GateAgent(schema_compliance)
└── BaseAgent("reviewer")
```

### Implementation Loop

Each iteration is one drafter call followed by its after-agent callback, which writes the draft into the run's work root and runs the stdlib, lint and correctness validators (the correctness validator executes the draft; see the README's containment note). A passing draft sets `implementation_passed`, and the `implementation_checkpoint` agent escalates out of the loop, so no further draft is requested. A failing draft leaves its issues in `validation_feedback`, which the drafter's instruction includes on the next iteration. `agents.max_implementation_retries` is the loop's `max_iterations`: the total number of drafts including the first. When every draft fails, the blocking gates fail the run.

### Artifact Generation

Three agents run concurrently:
- **Instrumentation** reads the implementation from session state and adds trace hooks
- **Visualization** generates a Manim animation scene (optionally using a scaffold template)
- **Assessment** generates Anki flashcards in CSV format

### Review

The reviewer is a programmatic agent, not a model: it runs the consistency and schema compliance validators once across all artifacts and records `review_verdict` (`passed` or `failed: …`). It never calls a model, so it has no budget share.

### Gate Verdicts

Each `GateAgent` computes one verdict entry (gate, stage, verdict, whether the gate blocks, diagnostics). It records the entry in the run's `BudgetTracker` and yields it as an `EventActions.state_delta` appended to `gate_verdicts`, so the session service stores it. PASS, WARN and a FAIL from a non-blocking gate continue. A FAIL from a blocking gate, including a gate that raised, then raises `BlockingGateError`; the error propagates through `SequentialAgent` and the Runner, so no later agent runs. (ADK copies each child's invocation context, so setting a flag on it would not stop the parent.)

The CLI runner catches only that error, reads the stored session back from the same session service and attaches it. `build`, `retry` and `scripts/integration_test.py` (all through `core/cycles.run_build`) then record the run once as `failed` with that state, the budget summary (gate verdicts plus the cycle's ledger reference), the elapsed time and an error naming the gate; the work-root files stay and nothing is sealed, so the run cannot be approved or submitted. Other exceptions are recorded as before. With an empty draft the correctness gate fails first, so such a run also ends `failed` with the gate's diagnostics.

The same recorded verdicts decide whether a run can be sealed and published. `complete_run` refuses to seal a budget summary that holds a blocking FAIL, and `approve` and `submit` refuse a completed run whose `budget_summary.gate_verdicts` include one (runs sealed before the halt existed). Verdicts recorded without the `blocking` flag come from the four pipeline gates, which all block; WARN and non-blocking FAIL verdicts do not.

Metering never replaces agent callbacks: it lives in each role's model client (see [Metered Model Calls](#metered-model-calls)), so the drafter's validation callback and the loop checkpoint run in the assembled pipeline.

### Packaging

Packaging is not an agent and is not part of the pipeline. `apprentice submit <algorithm> --run-id <run-id>` runs no model, generation graph, drafting or rendering:

1. The human-review gate (`gates/review.py`) requires a completed run, loads the run's sealed bundle once and verifies it (see [Run-Owned Artifacts](#run-owned-artifacts)), refuses a run whose build recorded a failed blocking gate, then requires an approval recorded by `apprentice approve <run-id>` and requires the approval, run record, bundle manifest and requested algorithm (and tier, if given) to agree. A run without a sealed bundle gets the rebuild instruction, not an approve remediation. Any mismatch, tampering, added or removed file, symlink or changed destination fails here, before any clone or other packaging side effect.
2. `agents/packaging.py` writes exactly those captured bytes to their manifest destinations (`no-magic/<tier dir>/micro<name>.py`, `no-magic-viz/scenes/scene_micro<name>.py`) in fresh clones of the two fixed repositories inside an exclusive scratch root, refusing existing destinations and symlinked directories. It stages only those paths, commits with the approval time as author and committer date, and checks that each commit contains exactly the approved bytes and paths before anything is pushed.
3. It pushes branch `apprentice/<run-id>` to both repositories and opens the PRs with `gh`, the viz PR referencing the core PR.

Each run gets one submission attempt, recorded on the run record. `approve` and `submit` serialize on a run with an exclusive advisory lock (`flock`) on `runs/<run-id>.lock`, an empty file that is created once and never replaced or removed. The lock is held only for short local steps (reading, checking and saving the record), never while cloning, pushing or calling `gh`. Under it, `submit` reloads the record, runs the review gate above (capturing the bundle bytes once), refuses any recorded attempt, allocates a fresh scratch root and saves the attempt as `pending` with the manifest digest, scratch workspace and branch. It then releases the lock and clones, pushes and opens PRs using only the captured bytes and the approval read under the lock. Afterwards it takes the lock again and, only if the stored attempt is still exactly the one it reserved, saves it as `complete` (both branches pushed, both PRs opened), `partial` (some branch was or may have been pushed; the record lists, per repository, whether its branch was pushed and the PR opened before the error) or `failed` (no branch was pushed), with the error. A push or `gh pr create` that times out may still have taken effect, so its `pushed` or `pr_url` is recorded as `null` (unknown) rather than false or empty, and an unknown push makes the attempt `partial`; a `git` or `gh` that cannot be started at all had no effect. The approval, the attempt (`submission`) and the gate verdicts in `budget_summary` are refused with an error, before any attempt is reserved, when they are not of the recorded shape (an object, a list of verdict objects, an integer approved tier); an empty `submission` object, or none, means no attempt yet. If the record was deleted or its attempt changed meanwhile, nothing is saved and `submit` exits non-zero, printing the effects it knows about and the discrepancy.

A run with any recorded attempt, including one left `pending`, is refused without touching a repository, and the stored attempt is printed as recorded: `submit` never retries, resumes or reconciles publication. The operator inspects the recorded effects. A process killed before the `pending` save leaves at most an empty scratch root, and the run can be submitted again; one killed after it leaves the attempt `pending` with its remote effects unknown. These guarantees cover a process being killed and restarted on one machine. Record writes are atomic renames without `fsync`, so nothing is claimed about power loss, and the lock does not coordinate processes on different machines.

`approve` takes the same lock and is refused once the run has an attempt, so the approval an attempt publishes cannot change; re-approving before any attempt replaces the approval. The approver (`--approver`, otherwise `$GITHUB_USER`, then `$USER`; an explicit value never falls back) must be a non-blank string without CR, LF or NUL and is stored exactly as given. The review gate refuses a stored approval whose approver breaks that rule or whose `approved_at` is not a canonical timezone-aware ISO 8601 timestamp, before any attempt is recorded.

Instrumented code and Anki cards are kept in the approved bundle but not promoted.

Packaging does not yet produce what released no-magic v3 requires beyond those two repos: a `no-magic-papers` card whose `implementations[]` references the script, and an explicit `SCRIPT_TO_PAPER` entry in `no-magic/scripts/generate_catalog.py`. The M1 catalog extension to that generator defines an additional per-script `SCRIPT_CONTRACTS` record (teaching kind, data source, adaptation note) that released v3 does not contain; when the target no-magic generator revision defines `SCRIPT_CONTRACTS`, packaging must populate it as well.

## Session State

ADK agents communicate through session state. Each agent writes to a key specified by `output_key`:

| Agent | output_key | Content |
|---|---|---|
| drafter | `generated_code` | Python source code |
| drafter callback | `validation_feedback`, `implementation_passed`, `implementation_path` | Issues for the next attempt; whether the draft passed |
| instrumentation | `instrumented_code` | Python source with trace hooks |
| visualization | `manim_scene_code` | Manim Scene class |
| assessment | `anki_deck_content` | CSV flashcard content |
| reviewer | `review_verdict` | Pass/fail with details |
| discovery | `discovery_candidates` | JSON array of candidates |

Agents read from other agents' keys using `{key_name}` in their instruction templates.

## Metered Model Calls

Every model-using command runs inside one controlled cycle (`core/cycles.py`): `build`, `retry`, standalone `suggest` and library builds such as `scripts/integration_test.py`. The route (backend, model, accounting profile, pinned SDKs and price data) is resolved before the cycle is admitted (`providers/factory.resolve_route`). Each agent gets its own `LiteLlm` whose `llm_client` is a `MeteredResponsesClient` bound to the cycle, stage and role (`metering/client.py`):

1. streaming, uncountable request fields, non-function tools and non-text input are refused before anything is reserved;
2. the chat request is transformed into Responses input once; the counting fee bound (a paid profile's, zero for non-hosted) is reserved and dispatch intent persisted before `POST /responses/input_tokens` counts that exact projection;
3. counted input plus the largest admissible output cap and its worst-case quote are reserved against the month, cycle, stage, role share and call ceilings in one ledger transaction, dispatch intent is persisted, and the same projection is sent to `POST /responses` with that cap, `store=false` and the standard tier;
4. the raw echoed model, tier and complete usage are checked and settled once before the SDK converts the response.

SDK and HTTP retries are disabled, environment proxies and redirects are ignored, and the paid endpoint is fixed. After dispatch, an error, cancellation, crash or missing usage keeps the full reservation as unknown exposure; inconsistent or above-bound usage is charged and quarantines the profile. The ledger, leases and recovery are described in [Configuration](configuration.md#budget-enforcement). `BudgetTracker` in `core/budget.py` now only collects gate verdicts; the run record's `budget_summary` holds them plus the cycle's ledger entries (`accounting`) as a derived reference — the ledger is the single authority.

`[rate_limits]` and `[circuit_breaker]` are parsed and validated but not yet enforced. ADK's `RunConfig(max_llm_calls=...)` per run remains an additional cap.

## Session Persistence

`SessionStore` in `core/session_store.py` persists run records as JSON files in `~/.apprentice/sessions/`. Each record captures:

- Session state (all agent outputs)
- Budget summary (gate verdicts and the cycle's ledger entries)
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

Every writer (`GateAgent`, the drafter's validation callback and the reviewer) uses the shared fixed role filenames in `core/artifacts.py` (`implementation.py`, `instrumented.py`, `scene.py`, `cards.csv`, `validation_report.json`, `discovery.json`) and refuses a missing or symlinked root and symlinked or multiply-linked files. No path is derived from the algorithm name, the current directory or a temporary directory.

Completing a run seals its final artifacts into `bundle/` with a `manifest.json`: canonical UTF-8 JSON (sorted keys, fixed separators) with the run ID, algorithm, integer tier and, per artifact, its role, file name, size, SHA-256 and repository destination (`null` when the role is kept but not promoted). The manifest digest is the SHA-256 of that JSON without its own `manifest_sha256` field. `preview`, `approve` and `submit` reload the bundle and reject a malformed or non-canonical manifest, changed identity or destinations, added or removed files, symlinks and any byte change. A run recorded before sealed bundles existed has no bundle; it fails with an instruction to rebuild rather than being regenerated.

The control authority lives beside the run records in `controls/` (`authority.id`, `accounting.sqlite3`, `leases/<slot>.lock`), outside the `*.json` record glob. A build registers its run ID with its cycle before the record is written; a record that no cycle references is treated as unmetered earlier work.

This enables:
- `apprentice retry <run-id>` — rerun failed pipelines
- `apprentice history` — list past runs
- `apprentice metrics` — run lifecycle plus usage by accounting category from the ledger for every controlled cycle, including record-less suggest/library cycles (historical estimates, qualified quotes, reference capacity, known-zero hosted usage and unknown holds, never summed)

## Model Routes

`providers/factory.py` resolves the one qualified route — `openai` (exactly `https://api.openai.com/v1`, `OPENAI_API_KEY`) or `local` (a loopback non-hosted server) — and checks that the accounting profile applies to exactly that backend and model. `--backend`/`--model` overrides go through the same resolution; nothing changes the model or provider automatically and nothing is written to the process environment. The legacy `core/pipeline.Pipeline`, `stages/*`, the unmetered `providers/{anthropic,openai,claude_cli}` adapters and the estimate-based `core/tokens.py`/`models/budget.py` were removed.
