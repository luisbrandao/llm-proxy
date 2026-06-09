# LLM Proxy

OpenAI-compatible reverse proxy for **multiple LLM backends**, with **priority-based
load balancing**, per-backend **concurrency limits (slots)**, automatic **failover**,
a bearer-key **permission gate**, **Prometheus metrics**, **token counting**, and
**structured logging**.

Drop it between your LLM clients (Open WebUI, OpenCode, LangChain, any OpenAI SDK, …)
and any number of upstream backends (DeepSeek, Ollama hosts, OpenRouter, …). Clients
request a **clean model name** — the proxy decides *which* backend actually serves it.

## Highlights

- **Transparent routing** — clients use a plain model name (`deepseek-v4-flash`,
  `local.qwen-medium:low`); no need to know which server runs it.
- **Priority + slots** — prefer your fast/cheap backend, cap concurrency per backend,
  and **queue** requests when every candidate is busy.
- **Failover** — if a backend errors, the request retries the next one; offline
  backends drop out of the model list and are skipped.
- **Permission gate** — mark paid backends `require_permission: true`; only callers
  with a valid `Authorization: Bearer` key see or use them.
- **OpenRouter provider routing** — pin which upstream OpenRouter uses (e.g. force
  DeepSeek-official instead of whatever's cheapest).
- **Response decompression** — transparently decodes gzip/deflate/brotli upstream
  bodies that a client couldn't otherwise read.

## Architecture

```
Your App (OpenAI client)
       │  POST /v1/chat/completions   { "model": "deepseek-v4-flash" }
       ▼
┌──────────────────────────────────────────────┐
│                  llm-proxy                     │
│  resolve model → ordered targets               │
│  auth gate → slot acquire (priority+queue)     │
│  forward → failover on error → decompress      │
│  log tokens · export metrics                   │
└───┬─────────────┬──────────────┬───────────────┘
    ▼             ▼              ▼
 OpenRouter    DeepSeek     Ollama hosts        (any backend in config.yaml)
 (10 slots)    (10 slots)   (1 slot each, citrine preferred over gw)
```

## How routing works

A client sends a **model name**. The proxy resolves it to an ordered list of
**targets** (a real model on a real backend), then admits the request to the
highest-priority target that has a free slot.

**Resolution order:**
1. **Alias** — a global short name → `provider:model` (e.g. `chat` → `deepseek:deepseek-chat`).
2. **Explicit `provider:model`** — forces one backend (manual override / pinning).
3. **Logical model** — an explicit `models:` entry mapping one client-facing name to
   several prioritized targets (used when the real ids differ across backends).
4. **Auto-group** — the same model id served by multiple backends is load-balanced
   automatically, ordered by each backend's `priority` (no config needed).
5. **Fallback** — first backend whose `enabled_models` lists it, else the first backend.

### Slots, priority & queueing

Each backend declares `slots` (max concurrent in-flight requests, shared across all its
models) and `priority` (lower = preferred). When a request arrives the proxy takes a
slot from the **highest-priority candidate that has one free**. If all candidates are
full it **waits** (indefinitely by default, or up to `routing.queue_timeout`) for the
first slot to free.

> Example: `local.qwen-medium:low` runs on `ollamaCitrine` (fast, 1 slot, priority 1)
> and `ollamaGW` (slow, 1 slot, priority 2). Request 1 → citrine, request 2 → gw,
> request 3 → waits for whichever frees first.

### Failover

If a chosen backend errors (connection refused, timeout, …) the proxy releases the slot,
marks the backend down for `routing.down_backoff` seconds, and retries the next-priority
target. Only if **all** candidates fail does the client get an error. Streaming requests
can fail over up to the first byte (after that the HTTP status is already committed).

## Configuration

All routing config lives in `config.yaml` (see `config.example.yaml`). Provider API keys
go inline, or optionally via `${ENV_VAR}` interpolation. Proxy auth keys and runtime
flags come from the environment.

```yaml
aliases:                          # optional short names -> provider:model
  chat: deepseek:deepseek-chat

providers:
  - name: deepseek
    api_key: "sk-..."             # or "${DEEPSEEK_API_KEY}"
    require_permission: true      # paid -> only authenticated callers
    base_url: "https://api.deepseek.com"
    enabled_models: [deepseek-v4-pro, deepseek-v4-flash]
    slots: 10
    cache_ttl: 3600
    model_map:
      deepseek-chat: deepseek-v4-pro

  - name: ollamaCitrine
    api_key: ""                   # empty -> no auth header (Ollama)
    base_url: "http://citrine.brandao:11434"
    enabled_models: []            # empty -> expose ALL models (live-discovered)
    slots: 1                      # single GPU box
    priority: 1                   # faster -> preferred over gw

  - name: ollamaGW
    base_url: "http://gw.brandao:11434"
    enabled_models: []
    slots: 1
    priority: 2

  - name: openRouter
    api_key: "sk-or-..."
    require_permission: true
    base_url: "https://openrouter.ai/api"
    enabled_models: [deepseek/deepseek-v4-flash, z-ai/glm-5.1]
    slots: 10
    provider_routing:             # pin OpenRouter's upstream choice
      deepseek/deepseek-v4-flash: [deepseek]

# One client-facing name backed by several prioritized targets (differing real ids).
models:
  deepseek-v4-flash:
    targets:
      - {provider: openRouter, model: deepseek/deepseek-v4-flash, priority: 1}
      - {provider: deepseek,   model: deepseek-v4-flash,         priority: 2}

# Global routing behavior.
routing:
  queue_timeout: 0    # seconds to wait for a free slot; 0 = wait forever
  failover: true      # retry the next target on backend error
  auto_group: true    # identical model ids across backends load-balance
  down_backoff: 15    # seconds a failed backend is skipped before retry
```

### Provider fields

| Field | Description |
|---|---|
| `name` | Backend key, also the `provider:` routing prefix |
| `base_url` | Upstream base URL |
| `api_key` | Sent as `Authorization: Bearer` upstream. Empty → no auth header (e.g. Ollama) |
| `enabled_models` | **Empty** = expose all (live-queried). **Non-empty** = exactly these (no live call) |
| `slots` | Max concurrent in-flight requests (shared across the backend's models). Omit = unlimited |
| `priority` | Preference when a model has several backends; lower wins. Default = config order |
| `require_permission` | `true` → gated behind a proxy auth key (default `false`) |
| `model_map` | Optional incoming→upstream model name rewrites |
| `provider_routing` | OpenRouter only — per-model upstream pinning (list = strict order, dict = verbatim `provider` field) |
| `cache_ttl` | Seconds the live `/models` result is cached for this backend |

### `models:` and `routing:`

- **`models:`** — declare logical models whose targets have *different real ids* per
  backend (the flash example). Backends sharing the *same* id auto-group without an entry.
- **`routing:`** — `queue_timeout`, `failover`, `auto_group`, `down_backoff` (see above).

### Environment variables

Set in `docker-compose.yml` / `.env` — **not** in `config.yaml`:

| Env var | Default | Description |
|---|---|---|
| `PROXY_API_KEYS` | _(empty)_ | Comma-separated proxy auth keys for gated backends. Empty = gate disabled |
| `LOG_INPUT` | `false` | Log the full proxied request (curl-style, auth masked) |
| `LOG_OUTPUT` | `false` | Log the upstream response (pretty JSON; streaming reassembled) |
| `PORT` | `8000` | Port the proxy binds to inside the container |
| `CONFIG_PATH` | `config.yaml` | Path to the YAML config |

> **Single worker required.** Slot/queue accounting is in-process, so run **one**
> uvicorn worker (the default). Multiple workers would split the accounting and break
> the concurrency caps.

## Authentication & permissions

Backends marked `require_permission: true` are gated. A request is **authorized** when it
carries `Authorization: Bearer <key>` with a key listed in `PROXY_API_KEYS`.

- **With a valid key** → sees and uses every backend.
- **Without a key** → gated backends are hidden from `/v1/models`, and requests to them
  (including explicit `provider:model` pins) return **401**. Open backends work for everyone.
- **No keys configured** → the gate is inert; everything is open.

> Use case: share your Ollama hosts with a friend (no key, open) while keeping your paid
> DeepSeek/OpenRouter backends private (key required).

## Quick Start

```bash
cp .env.example .env                 # set PROXY_API_KEYS
cp config.example.yaml config.yaml   # edit backends, slots, priorities, keys
docker compose up -d
```

Point any OpenAI client at `http://<host>:8000/v1/chat/completions` and request a clean
model name (e.g. `deepseek-v4-flash`). Pass `Authorization: Bearer <key>` for gated backends.

## Endpoints

| Path | Method | Description |
|---|---|---|
| `/health` | `GET` | `{"status": "ok"}` health check |
| `/metrics` | `GET` | Prometheus-format metrics |
| `/models`, `/v1/models` | `GET` | Aggregated model list (clean names; honors the auth gate) |
| `/*` | any | Catch-all proxy — routed from the request body's `model` |

### `/models` aggregation

Lists, deduplicated: aliases, logical models, and each bare model id once. A model served
by several backends appears a **single** time (the proxy load-balances behind it).
Backend-prefixed `provider:model` ids are **not** listed — they still work for pinning a
specific backend, but advertising them would just duplicate the clean names. Live-discovered
backends are queried (`GET {base_url}/v1/models`), cached for `cache_ttl`, coalesced via
single-flight so a burst of cold requests triggers one probe per backend; offline backends
drop out. Gated backends are hidden from callers without a valid key.

## Prometheus Metrics

Scrape `http://<host>:8000/metrics`:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `llm_proxy_requests_total` | Counter | `provider`, `model` | Completed requests |
| `llm_proxy_tokens_input_total` | Counter | `provider`, `model` | Cumulative input tokens |
| `llm_proxy_tokens_output_total` | Counter | `provider`, `model` | Cumulative output tokens |
| `llm_proxy_request_duration_seconds` | Histogram | `provider`, `model` | Request latency (0.1–600s) |
| `llm_proxy_errors_total` | Counter | `provider`, `model`, `status_code` | Failed requests |
| `llm_proxy_slots_in_use` | Gauge | `provider` | In-flight requests holding a slot |
| `llm_proxy_queue_waiting` | Gauge | — | Requests currently waiting for a slot |
| `llm_proxy_failovers_total` | Counter | `provider` | Failovers away from a backend |

> **Metric prefix:** as of the multi-backend rework these use `llm_proxy_` (was
> `deepseek_proxy_`). Update existing Grafana/alert queries accordingly.

### Persistence across restarts (optional)

In-memory counters reset to zero on restart, which Prometheus sees as a counter reset and
loses the delta around the reboot. Enable **`METRICS_PERSIST=true`** to snapshot the
cumulative counters to `METRICS_PERSIST_PATH` (lazily, every `METRICS_FLUSH_INTERVAL`
seconds and on graceful shutdown) and re-seed them on boot, keeping totals continuous.

Only counters are persisted; live gauges (`slots_in_use`, `queue_waiting`) and the latency
histogram intentionally reset. **The path must be on a volume that outlives the
container** (the bundled `docker-compose.yml` mounts `./data`), otherwise the file is
recreated empty each restart and persistence is a no-op.

| Env var | Default | Description |
|---|---|---|
| `METRICS_PERSIST` | `false` | Enable counter persistence |
| `METRICS_PERSIST_PATH` | `metrics_state.json` | Where the snapshot is written (use a mounted volume) |
| `METRICS_FLUSH_INTERVAL` | `30` | Seconds between lazy snapshots |

## Token Logging

Every completed request emits one line:

```
2026-06-09 14:19:03 - INFO - Tokens - Provider: deepseek, Model: deepseek-v4-pro, In: 29323, Out: 142, Time: 0:00:10, Speed: 13.76 t/s
```

`LOG_INPUT` logs the full proxied request curl-style (auth masked); `LOG_OUTPUT` logs the
upstream response pretty-printed (streaming reassembled into one JSON with
`_assembled_content` / `_reasoning_content`). uvicorn's access/error logs are reformatted
to match this `timestamp - level - message` style.

> **Streaming token counts:** upstreams only emit a `usage` block in a streamed response
> when the request sets `stream_options: {"include_usage": true}`. The proxy **injects
> this automatically** on streamed requests (unless the client explicitly set it), so
> token metrics/logs are reliable for streams too. As a result the client receives a final
> usage chunk (standard OpenAI streaming behavior). Non-streaming always reports usage.

## CI / CD

Pushes to `master` (ignoring `.md`-only changes) build the Docker image and push to GitHub
Container Registry, tagged `latest` and `master-{run_number}`, using `GITHUB_TOKEN`.

## Tech Stack

- **FastAPI** + **uvicorn** — async ASGI
- **httpx** — async client with streaming
- **prometheus-client** — native metrics
- **PyYAML** — config
- **Python 3.11**

## License

MIT
