# Troubleshooting

## API key errors

```
RuntimeError: Backend 'anthropic' requires environment variable ANTHROPIC_API_KEY to be set
```

Set the required key in your `.env` file or environment:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Build produces no output

Check `apprentice history` for the run status and error:
```bash
apprentice history --status failed
```

Common causes:
- Model returned empty or malformed response
- All 3 implementation retries failed validation
- Budget exhausted mid-pipeline

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

## Ollama model not found

```bash
# List available models
ollama list

# Pull the model
ollama pull llama3.3
```

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
