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

Open PRs with the exact bytes a human approved. No model is called and nothing is regenerated or rendered.

```
apprentice submit <algorithm> --run-id ID [--tier N]
```

- `algorithm` — algorithm the approved run built; must match the run
- `--run-id` — approved run to submit (required)
- `--tier` — if given, must match the run's tier

Verifies the approval, run identity and sealed bundle before any repository is cloned, then promotes the approved implementation to `no-magic/<tier dir>/micro<name>.py` and the approved scene to `no-magic-viz/scenes/scene_micro<name>.py` on branch `apprentice/<run-id>` and opens a PR in each repository with `gh`. Uses the operator's `git` and `gh` credentials. Under a short per-run lock it records the attempt on the run as `pending` before any clone; it ends `complete`, `partial` (lists, per repository, whether the branch was pushed and the PR opened before the error; `null` when a push or `gh pr create` timed out and its effect is unknown) or `failed` (no branch pushed), recorded only if the stored attempt is still the one it reserved (otherwise it exits non-zero and prints the effects it knows about). A run with any recorded attempt is refused without touching a repository and its stored attempt is printed; there is no retry or resume. A run whose build recorded a failed blocking gate is refused, and a run without a sealed bundle gets the rebuild instruction. See [Architecture: Packaging](architecture.md#packaging).

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
- `--approver` — approver identity (default: `$GITHUB_USER`, then `$USER`); must be non-blank without CR, LF or NUL, and an invalid explicit value is refused rather than replaced by the default

Verifies the run's sealed bundle and stores the run ID, algorithm, tier, manifest digest, approver and time on the run record; prints every artifact's role, size, SHA-256 and destination. Fails for a tampered bundle, for a run whose build recorded a failed blocking gate and for a run that already has a submission attempt (its approval is then fixed); for a run without a sealed bundle, asks for a rebuild. Re-approving before any attempt replaces the approval. The approval is a local operator attestation, not a cryptographic identity.

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
