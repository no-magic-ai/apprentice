# Troubleshooting

## Route not qualified

Commands that call a model print `{"error": ..., "denied_before": "any cycle admission or request"}` when the route cannot be used. `apprentice status` shows the same check as `route.structurally_valid`, and `route.admissible`/`route.blocked_by` add what the ledger refuses for that profile now (quarantine, unknown month, suspension, legacy records, concurrent items, cooldown, circuit).

- `no accounting profile is configured` — set `[provider].accounting_profile_path` to an operator-qualified profile ([Configuration](configuration.md#accounting-profile)). apprentice ships none.
- `backend '<name>' was removed` / `[provider].fallback_model was removed` — only `openai` and `local` remain; delete removed keys ([Configuration](configuration.md#removed-settings)).
- `qualifies model ..., not ...` — the configured or `--model` value must be the profile's `requested_model`.
- `backend 'openai' requires the OPENAI_API_KEY environment variable` — export it in the shell that runs `apprentice`; the CLI does not load `.env` files.
- `environment variable OPENAI_BASE_URL=... conflicts with the route endpoint` — unset it; routes are never redirected through the environment.
- `installed <sdk> ... is not the pinned ...` or a price-data hash mismatch — run `uv sync` so the pinned `google-adk`, `litellm` and `openai` are installed.

## Call or cycle denied

Denials print `"outcome": "denied"` and the limiting `control`:

- a budget key (for example `budget.stage.max_tokens_per_stage`) — that scope has no room left for the counted input plus at least one output token. Committed and unknown usage are never refunded; wait for the next cycle or UTC month, or raise the limit (a raised or lowered limit takes effect once no cycle is live).
- `budget.global` with `usage in <month> is unknown` — earlier apprentice state or a run record written without the control authority holds that month; it is released only by the next UTC month.
- `controls.legacy` — earlier records may still be in progress; once no earlier apprentice process runs, `apprentice controls adopt-legacy --operator <name> --declare-no-earlier-process-running`.
- `rate_limits.max_concurrent_items` — another controlled cycle (build, retry, suggest, submit) is running.
- `rate_limits.cooldown_hours` — the previous cycle was admitted less than `cooldown_hours` ago; the message gives the next admissible time.
- `rate_limits.max_prs_per_day` / `max_prs_per_week` — the rolling window has no slot for every repository of the submission; nothing was claimed and the approval is kept.
- `rate_limits.max_files_per_pr` / `max_lines_per_pr` — a prepared commit exceeds the limit; nothing was pushed, the attempt is recorded with `"remote_write_intent": false` and may be submitted again once eligible.
- `circuit_breaker.*` — automated work failed repeatedly; wait for the probe time in `apprentice status`, or after investigating a latched circuit run `apprentice controls reset-circuit --operator <name>`.
- `controls.continuity` — `controls prepare-rollback` suspended the authority; after upgrading again run `apprentice controls adopt-legacy`.
- `controls.policy` — another process is running a cycle under a different configuration; retry after it finishes.
- `provider.accounting_profile_path ... quarantined` — a response's usage or echoed model contradicted the profile; inspect the ledger receipt and replace the profile.

`control authority unavailable` means the ledger in `controls/` is partial, foreign, corrupt (failed integrity check, a missing or altered table or column, missing or unreadable required metadata such as `last_clock`, `continuity` or the effective policy), of another schema version, or the clock is earlier than its last decision; for the current schema-3 ledger the circuit, cooldown, PR-window and suspension metadata are required and typed too. An older ledger is checked as its own version — schema 1 (no publication state) or schema 2 (publication state with one `known_submissions` row per run) — and upgraded in place to schema 3 in one transaction that keeps every row; schema-2 `known_submissions` rows become one row per registered submission (a foreign submission already booked keeps its booking; other rows are kept as history markers, so a later foreign submission of the run is still counted once). If the upgrade of a valid older ledger cannot complete (read-only file, a lock held by another apprentice process, an I/O fault) it is rolled back and the error says the ledger is unchanged — nothing needs restoring: clear the cause and run the command again. Only real damage (failed integrity check, wrong tables or metadata) is reported with "restore it from backup"; such a ledger is never upgraded. Every command that admits work, reports usage or recovers cycles checks this when it opens the ledger, and failures during admitted work are reported the same way; a cycle whose terminal state cannot be committed keeps its lease until the process ends, after which its dispatched calls stay held as unknown. Nothing is admitted; restore the files from backup — they are never recreated. A marker, ledger or `controls/`/`leases/` directory that cannot be read or created (permissions, a file where a directory belongs, a failed lock, a marker that is not UTF-8) is reported the same way with the operating-system error; fix the permissions or the misplaced file — nothing was changed. The same applies when a parent of the store or log roots (for example `~/.apprentice`) cannot be searched, so whether earlier apprentice state exists cannot be told: nothing is created until its search permission is restored. A run-record store problem is reported with the offending path and the operating-system error, exit 1, and no record is replaced; which message appears depends on what the command needs. Listing the store (`history`, `metrics`, `preview` without `--run-id`) fails in a store root that cannot be listed (no read permission, for example mode 0o300 or 0o100) or searched: `run store unavailable: run store <dir> cannot be used: cannot list its run records ...`. A known run is checked and read by its ID, which needs only search permission: in a store root without search permission (0o000), or with a record file that cannot be read, `retry` prints `run store unavailable: run store <dir> cannot be used: cannot check|read run record <path> ...`, and `approve`, `preview --run-id` and `submit` print the same `run store <dir> cannot be used: ...` error together with the `run_id`. In a store that can be searched but not listed, such a known run is read successfully, but every admission (`retry`, `build`, `suggest`, `submit`), `status` and the schema-2 upgrade scan all records through the control authority first and report `control authority unavailable: cannot list run records in <dir>` (or `cannot read run record <path>`), admitting nothing. Writing into the store — `approve` saving its approval, `submit` reserving its claim and work root, a build creating its run — fails in a store root without write permission (for example mode 0o500, or 0o100 for `approve`, which does not list the store; `submit` is refused by its admission scan first in a store that cannot be listed) with `run store <dir> cannot be used: cannot write run record <path>: ...` or `cannot create <path>: ...` (`approve` and `submit` with the `run_id`; `build` and `retry` as `run store unavailable: ...`, after the run's cycle has ended `failed`); the stored record keeps its previous state, though a temporary file or an empty lock or work directory of this process may remain. A runs root that is not a plain owned directory (for example a symlink) is refused the same way when a build creates its run. If a build's run record cannot be written when the build ends, its cycle still ends with the outcome it reached (`cancelled` or `failed` after a SIGTERM, otherwise `failed`); the record keeps its previous state (for example `in_progress`) and the build's `error` — or, after a SIGTERM, the termination `error` — names the record and the operating-system error.

`submit` failing with `resolves to fetch ... instead of its supported URL` means the operator's git configuration rewrites or adds a destination for a supported repository (`url.<base>.insteadOf`, `pushInsteadOf`, an extra push URL); nothing was cloned from, committed for or pushed to it after the check. Remove that rewrite; normal git credentials are unaffected.

## Build produces no output

Check `apprentice history` for the run status and error:
```bash
apprentice history --status failed
```

Common causes:
- Model returned empty or malformed response
- Every implementation attempt (`agents.max_implementation_retries`, including the first) failed validation
- A budget ceiling denied a model call (`"outcome": "denied"`)

Retry with:
```bash
apprentice retry <run-id>
```

## Validators fail on valid-looking code

The lint validator requires:
- Module-level docstring
- Docstrings on all public functions
- Full type annotations on all parameters and return types
- No wildcard imports
- Under 500 lines

The correctness validator requires:
- An `if __name__ == "__main__":` block
- Clean exit (return code 0) within 5 seconds

## Session state issues

Run records are stored in `~/.apprentice/sessions/`. To reset:
```bash
rm -rf ~/.apprentice/sessions/
```

Logs are in `~/.apprentice/logs/apprentice.jsonl`.

## ADK dev UI not starting

```bash
# Verify adk is installed
uv run adk --version

# Try a different port
apprentice dev --port 9090
```

## Local server refused

A `local` route needs a loopback IP `local_api_base` and a server implementing the counted Responses protocol for the profiled model ([Local Models](local-models.md)). A plain chat-completions server (for example Ollama's API) is not enough.

## Integration test failures

```bash
# Run with dry-run to verify setup
uv run python scripts/integration_test.py --dry-run

# Run a single tier
uv run python scripts/integration_test.py --tier 1 --limit 1
```

## mypy or ruff errors after changes

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy --strict src/apprentice/
```
