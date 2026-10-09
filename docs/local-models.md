# Local (Non-Hosted) Models

A local model is used through `backend = "local"` only when it is metered exactly like a hosted one: every request is counted, reserved, sent and settled through the installation ledger. Ollama's chat API, llama.cpp's server and other plain OpenAI-compatible chat endpoints do not qualify by themselves; the earlier `ollama` backend and its environment-variable configuration were removed. apprentice never falls back to another model or provider.

## Server requirements

The server must:

- listen on a loopback IP address (`127.0.0.1` or `::1`); `local_api_base` carries no credentials, query or fragment, and requests ignore environment proxies and redirects;
- implement `POST <local_api_base>/responses/input_tokens` and `POST <local_api_base>/responses` (the OpenAI Responses protocol) for the same complete input — instructions, input items and function tools — counted by the model's own tokenizer;
- honour `max_output_tokens` as a cap on all generated output, including any hidden reasoning;
- echo the requested model ID and report complete usage: `input_tokens`, `output_tokens`, `total_tokens`, plus `input_tokens_details.cached_tokens` and `output_tokens_details.reasoning_tokens` when the profile declares those subsets.

A server that lacks any of this is refused before a request is sent, or — if it answers with an unqualified model or incomplete usage — its reservation is kept as unknown usage and its profile is quarantined.

## Configure apprentice

```toml
[provider]
backend = "local"
model = "openai/<the server's model ID>"
local_api_base = "http://127.0.0.1:8080/v1"
accounting_profile_path = "local-profile.json"
```

`local-profile.json` is a `non-hosted-responses` profile (see [Configuration](configuration.md#non-hosted-route-non-hosted-responses)). In it the operator declares that the server is genuinely non-hosted and states the model's own context window and output maximum; no GPT context or tokenizer is assumed. Choose the cost policy:

- `zero-hosted` — hosted cost is known to be zero; tokens still count against every token ceiling;
- `sdk-reference-capacity` with `reference_rate_model` — USD ceilings are charged at a separate priced model's pinned rates as modelled capacity, reported apart from hosted spend.

## Run

```bash
apprentice status                      # route validity/admission and ledger state
apprentice build "insertion_sort" --tier 1
```

Small local models fail validation more often; each failed draft is retried up to `agents.max_implementation_retries` attempts in total, and every attempt is metered.
