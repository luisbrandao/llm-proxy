# LLM Proxy

OpenAI-compatible reverse proxy for **multiple LLM providers**, with built-in **Prometheus metrics**, **token counting**, and **structured logging**.

Drop it between your LLM clients (Open WebUI, OpenCode, LangChain, any OpenAI SDK, etc.) and any number of upstream providers (DeepSeek, Ollama hosts, OpenRouter, …). One endpoint, one set of metrics, every provider.

## Architecture

```
Your App (OpenAI client)
       │
       │  POST /v1/chat/completions   { "model": "deepseek:deepseek-chat" }
       ▼
┌─────────────────┐
│    llm-proxy     │  ← routes by provider prefix, adds auth, logs tokens, exports metrics
│    :8000         │
└───┬────┬────┬───┘
    │    │    │
    ▼    ▼    ▼
 DeepSeek  Ollama  OpenRouter   (any provider in config.yaml)
```

## How routing works

Clients address a model as **`provider:model`**. The part before the first `:` selects the provider from `config.yaml`; the rest is the real model name sent upstream.

```
deepseek:deepseek-chat        → deepseek provider, model deepseek-chat (then model_map → deepseek-v4-pro)
ollamaGW:llama3               → ollamaGW host
ollamaCitrine:llama3          → ollamaCitrine host   (same model name, different host — unambiguous)
openRouter:openai/gpt-4o      → openRouter
```

Resolution order:
0. Expand a **global alias** (simple name → `provider:model`).
1. Explicit `provider:` prefix.
2. Bare name listed in some provider's `enabled_models`.
3. Fallback to the first provider in the config.

Each provider can also declare a `model_map` to rewrite incoming names (e.g. `deepseek-chat` → `deepseek-v4-pro`).

### Global aliases

For short, provider-agnostic names, define top-level `aliases` mapping a simple name to a `provider:model` target:

```yaml
aliases:
  chat: deepseek:deepseek-chat
  reasoner: deepseek:deepseek-reasoner
  llama: ollamaCitrine:llama3
  sonnet: openRouter:anthropic/claude-3.5-sonnet
```

Now a client can just request `model: "chat"`. Aliases resolve **before** provider routing, so they still pass through the target provider's `model_map` (`chat` → `deepseek:deepseek-chat` → `deepseek-v4-pro`). Aliases are also listed in `/models` under their simple name, so they're discoverable by tools like OpenCode.

## Configuration

All config lives in `config.yaml` (see `config.example.yaml`). API keys support `${ENV_VAR}` interpolation so secrets stay in `.env`.

```yaml
aliases:                       # optional short names -> provider:model
  chat: deepseek:deepseek-chat
  sonnet: openRouter:anthropic/claude-3.5-sonnet

providers:
  - name: deepseek
    api_key: "${DEEPSEEK_API_KEY}"
    base_url: "https://api.deepseek.com"
    enabled_models: [deepseek-v4-pro, deepseek-v4-flash]
    cache_ttl: 3600
    model_map:
      deepseek-chat: deepseek-v4-pro
      deepseek-reasoner: deepseek-v4-flash

  - name: ollamaGW
    api_key: ""
    base_url: "https://gw.brandao:11434"
    enabled_models: []      # empty = expose ALL models (live-discovered)
    cache_ttl: 10
```

| Field | Description |
|---|---|
| `name` | Provider key used as the `provider:` routing prefix |
| `base_url` | Upstream base URL |
| `api_key` | Sent as `Authorization: Bearer`. Empty → no auth header (e.g. Ollama) |
| `enabled_models` | **Empty** = expose all models (live-queried). **Non-empty** = expose only these (no live call) |
| `cache_ttl` | Seconds the live `/models` result is cached for this provider |
| `model_map` | Optional incoming→upstream model name rewrites |

Runtime flags are **environment variables** (set in `docker-compose.yml`), not part of `config.yaml`:

| Env var | Default | Description |
|---|---|---|
| `LOG_INPUT` | `false` | Log the full proxied request (curl-style, auth masked) |
| `LOG_OUTPUT` | `false` | Log the upstream response (pretty JSON; streaming reassembled) |
| `PORT` | `8000` | Port the proxy binds to inside the container |
| `CONFIG_PATH` | `config.yaml` | Path to the YAML config |

## Quick Start

```bash
cp .env.example .env              # set DEEPSEEK_API_KEY / OPENROUTER_API_KEY
cp config.example.yaml config.yaml # edit providers
docker compose up -d
```

Point any OpenAI client at `http://<host>:8001/v1/chat/completions` and use `provider:model` model names.

## Endpoints

| Path | Method | Description |
|---|---|---|
| `/health` | `GET` | `{"status": "ok"}` health check |
| `/metrics` | `GET` | Prometheus-format metrics |
| `/models`, `/v1/models` | `GET` | Aggregated model list across all providers, each id prefixed with `provider:` |
| `/*` | any | Catch-all proxy — routed to the provider resolved from the request body's `model` |

### `/models` aggregation

The proxy fans out to every provider and merges the results into one OpenAI-shaped list. Global aliases are listed first under their simple name. Providers with `enabled_models: []` are live-queried (`GET {base_url}/v1/models`) and cached for `cache_ttl` seconds; providers with an explicit list contribute exactly those models with no live call. Every provider-sourced id is prefixed (`deepseek:deepseek-v4-pro`, `ollamaGW:llama3`, …) so clients pick one unambiguously.

## Prometheus Metrics

Scrape `http://<host>:8001/metrics`:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `deepseek_proxy_requests_total` | Counter | `provider`, `model` | Total completed requests |
| `deepseek_proxy_tokens_input_total` | Counter | `provider`, `model` | Cumulative input tokens |
| `deepseek_proxy_tokens_output_total` | Counter | `provider`, `model` | Cumulative output tokens |
| `deepseek_proxy_request_duration_seconds` | Histogram | `provider`, `model` | Request latency (buckets: 0.1–600s) |
| `deepseek_proxy_errors_total` | Counter | `provider`, `model`, `status_code` | Failed requests |

> Metric names keep the `deepseek_proxy_` prefix for backward compatibility with existing dashboards; the new `provider` label distinguishes upstreams.

## Token Logging

Every completed request emits a single log line:

```
2026-05-29 14:19:03 - INFO - Tokens - Provider: deepseek, Model: deepseek-v4-pro, In: 29323, Out: 142, Time: 0:00:10, Speed: 13.76 t/s
```

`LOG_INPUT` logs the full proxied request curl-style (auth masked); `LOG_OUTPUT` logs the upstream response pretty-printed (streaming responses are reassembled into one JSON with `_assembled_content` / `_reasoning_content`).

> **Streaming token counts:** like the OpenAI API, upstreams (DeepSeek, Ollama, …) only emit a `usage` block in a streaming response when the client sends `stream_options: {"include_usage": true}`. Without it, token metrics/logs for streamed requests will be `0` (the content still streams fine). Non-streaming requests always report usage.

## CI / CD

Pushes to `master` (ignoring `.md`-only changes) build the Docker image and push to GitHub Container Registry, tagged `latest` and `master-{run_number}`. Uses the built-in `GITHUB_TOKEN`.

## Tech Stack

- **FastAPI** + **uvicorn** — async ASGI
- **httpx** — async client with streaming
- **prometheus-client** — native metrics
- **PyYAML** — config
- **Python 3.11**

## License

MIT
