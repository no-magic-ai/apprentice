# Local Model Setup

apprentice supports local LLMs via Ollama and any OpenAI-compatible API server.

## Ollama

### Install

```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh
```

### Pull a model

Minimum recommended model size for acceptable quality: **8B parameters** (e.g. llama3.1:8b).
For better results: **70B parameters** (e.g. llama3.3:70b).

```bash
ollama pull llama3.3
ollama pull llama3.1:8b    # smaller, faster, lower quality
```

### Configure apprentice

```toml
[provider]
backend = "ollama"
model = "ollama_chat/llama3.3"
fallback_model = "ollama_chat/llama3.1:8b"
local_api_base = "http://localhost:11434"
```

### Run

```bash
# Start Ollama server (if not running as service)
ollama serve

# Build with Ollama
apprentice build "insertion_sort" --tier 1

# Or override at runtime
apprentice build "insertion_sort" --backend ollama --model ollama_chat/llama3.3
```

## OpenAI-compatible servers (llama.cpp, vLLM, etc.)

Any server exposing an OpenAI-compatible API works with the `local` backend.

### llama.cpp server

```bash
# Start llama.cpp server
./llama-server -m models/llama-3.3-70b.gguf --port 8000 --host 0.0.0.0
```

### Configure apprentice

```toml
[provider]
backend = "local"
model = "openai/local-model"
fallback_model = "openai/local-model"
local_api_base = "http://localhost:8000/v1"
```

The `local` backend automatically sets `OPENAI_API_KEY=not-needed` and `OPENAI_API_BASE` from `local_api_base`.

## Quality expectations

Local models produce lower quality output than cloud models. The validators apply the same thresholds regardless of model — expect higher failure rates with smaller models:

| Model size | Expected success rate | Recommended tier |
|---|---|---|
| 8B | 60-70% | Tier 1 only |
| 13B | 70-80% | Tier 1-2 |
| 70B | 80-90% | Tier 1-3 |
| Cloud (Claude/GPT) | 95%+ | All tiers |

The implementation loop retries up to 3 times. With a 70B model, most tier 1-2 algorithms succeed within 2 attempts.

## Troubleshooting

### Ollama connection refused

Verify the server is running:
```bash
curl http://localhost:11434/api/tags
```

### Model too slow

Reduce token budget in `apprentice.toml`:
```toml
[budget.cycle]
max_tokens_per_cycle = 50_000
```

Or use a quantized model:
```bash
ollama pull llama3.1:8b-q4_0
```

### Out of memory

Use a smaller model or enable GPU offloading:
```bash
OLLAMA_NUM_GPU=999 ollama serve
```
