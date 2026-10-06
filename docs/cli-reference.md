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

Rejects algorithm names other than 1-64 lowercase letters, digits and underscores (starting with a letter) and tiers other than 1-4. Persists the run record to `~/.apprentice/sessions/` and, on completion, seals the final artifacts into the run's immutable bundle (see [Architecture: Run-Owned Artifacts](architecture.md#run-owned-artifacts)).

### submit

Regenerate an approved run and hand the regenerated files to the model-driven publisher, which opens PRs in `no-magic` and `no-magic-viz`.

```
apprentice submit <algorithm> [--run-id ID] [--tier N] [--backend NAME] [--model STRING]
```

- `algorithm` — must equal the approved run's algorithm
- `--run-id` — run to submit (from `apprentice history`); defaults to the most recently started completed run of `algorithm` among the 50 most recent completed runs
- `--tier` — optional assertion; when given it must equal the approved run's tier
- `--backend`, `--model` — as for `build`; they select the model that regenerates the artifacts and drives the publisher

Before resolving a model or allocating any work root, `submit` verifies the run's sealed bundle (see [Architecture: Run-Owned Artifacts](architecture.md#run-owned-artifacts)) and requires an approval recorded with `apprentice approve` whose run ID, algorithm, tier and manifest digest equal that bundle. A run without a sealed bundle (it must be rebuilt), a bundle that fails verification, a missing or mismatched approval, or a conflicting algorithm or `--tier` prints a JSON error and exits 1 without changing the run. The approved name and tier, never a default, then drive the regeneration, the prompts and the publisher destinations `no-magic/<tier dir>/micro<name>.py` and `no-magic-viz/scenes/scene_micro<name>.py`.

`submit` re-runs the model pipeline into a fresh root under `~/.apprentice/sessions/scratch/`. The review gate compares every regenerated role with the approved hashes; a changed, missing or added role stops the run with a JSON `gate` error before the publisher. When every role matches, the publisher receives the regenerated files' paths (not the sealed bundle) and uses its clone, branch, place-file and PR tools with the operator's ambient `git` and `gh` credentials. The publisher is model-driven, so `submit` does not guarantee that exactly the approved bytes are published, and it does not record a submission on the run.

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

### approve

Record a human-review approval for a completed run.

```
apprentice approve <run_id> [--approver NAME]
```

- `run_id` — exact ID from `apprentice history`
- `--approver` — approver identity (default: `$GITHUB_USER`, then `$USER`)

Verifies the run's sealed bundle and stores the run ID, algorithm, tier, manifest digest, approver and time on the run record; prints every artifact's role, size, SHA-256 and destination. Fails for a tampered bundle and, for a run without a sealed bundle, asks for a rebuild. The approval is a local operator attestation, not a cryptographic identity.

### preview

Inspect a completed run's sealed artifact bundle.

```
apprentice preview [--run-id ID]
```

- `--run-id` — run to preview (default: the most recently started completed run)

Verifies the bundle against its manifest and prints the run identity, manifest digest, approval state and, per artifact, its role, size, SHA-256, destination and first 500 characters.

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
