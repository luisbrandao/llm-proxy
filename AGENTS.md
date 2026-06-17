# AGENTS.md

Guidance for AI agents working in this repo. Read this before changing routing,
concurrency, or auth code. User-facing docs live in `README.md`.

## What this is

An OpenAI-compatible reverse proxy (FastAPI + httpx) in front of multiple LLM
backends. Clients send a clean model name; the proxy resolves it to one or more
prioritized backends, gates by permission, load-balances across per-backend
concurrency slots (queueing when full), forwards, fails over on error, and
decompresses the response. Single process, async, one uvicorn worker.

## Module map

| File | Responsibility |
|---|---|
| `app/config.py` | Loads `config.yaml` + env. Dataclasses `Provider`, `Target`, `LogicalModel`, `Routing`. Exposes `PROVIDERS`, `PROVIDERS_BY_NAME`, `ALIASES`, `LOGICAL_MODELS`, `ROUTING`, `AUTH_KEYS`. |
| `app/router.py` | `resolve(model) -> [Target]` (async). Resolution order: alias → `provider:model` → `models:` logical → auto-group → fallback. |
| `app/slots.py` | Per-provider concurrency. `acquire(targets, timeout)` / `release(provider)` / `slot()` ctx mgr. Priority admission + queue via a lazily-created `asyncio.Condition`. |
| `app/registry.py` | `/v1/models` listing, live model discovery (cached, single-flight), and backend health (`mark_down`/`is_down`/`clear_down`). |
| `app/auth.py` | Bearer-key gate: `is_authorized(request)`, `restricted(provider)`. |
| `app/proxy.py` | Request lifecycle: parse → resolve → gate → `_dispatch` (acquire slot, build body, forward, failover) → `_handle_non_stream` / `_handle_stream`. Also decompression + upstream error mapping. |
| `app/metrics.py` | Prometheus counters/gauges (`llm_proxy_` prefix) + `PERSISTABLE_COUNTERS`. |
| `app/persistence.py` | Optional: snapshot/restore cumulative counters to disk (`load`/`dump`/`flush_loop`). |
| `app/main.py` | FastAPI app, routes, logging unification, lifespan (metrics load/flush/dump). |

## Request lifecycle (`proxy.proxy_request`)

1. Parse body; extract `model`, `stream`. No model → passthrough to first provider (auth-gated, no slots).
2. `authorized = auth.is_authorized(request)`.
3. `router.resolve(model)` → ordered targets.
4. Gate: if unauthorized, drop `require_permission` targets; none left → **401**.
5. Drop currently-down targets (keep as last resort).
6. `_dispatch`: loop — `slots.acquire` → `_build_body` (rewrite model id, inject `provider_routing`) → forward. On `httpx.RequestError`: release slot, `mark_down`, try next target. Exhausted → `_backend_error`.

## Invariants — do not break these

- **Single worker.** Slot/queue/health state is in-process. Never add `--workers > 1`
  without moving that state to a shared store.
- **Every acquired slot must be released exactly once.** Non-stream: released in
  `_dispatch` after the call. Stream: released in the generator's `finally` via the
  `on_complete` callback (the handler returns before streaming finishes). If you add a
  code path, guarantee release on every exit including errors and client disconnect.
- **Async primitives are lazily created** (`slots._condition()`, `registry._lock_for`)
  so they bind to uvicorn's running loop, not the import-time loop. Do not move them to
  module-level construction — it breaks under some Python/loop setups.
- **Streaming fails over only pre-first-byte.** `_handle_stream` pre-flights the
  connection (`stream_cm.__aenter__`) and re-raises `RequestError` so `_dispatch` can try
  the next target *before* a `StreamingResponse` commits its 200 status. Don't move the
  error handling inside the body generator.
- **Handlers raise, the dispatcher decides.** `_handle_non_stream` / `_handle_stream`
  must let `httpx.RequestError` propagate (for failover). Only `_dispatch` /
  `proxy_request` convert errors to client responses (`_backend_error`, 401, 503).
- **Decompression reads raw bytes.** `_handle_non_stream` uses `aiter_raw()` + manual
  `_decompress` because httpx can't decode brotli without the lib. The forwarded
  `Accept-Encoding` is capped to `gzip, deflate` in `_build_headers`. Keep these aligned.
- **Auth gate consistency.** Any new model-listing or routing path must apply the same
  `require_permission` filtering as `registry.list_models` and `proxy_request`.
- **Metric names use the `llm_proxy_` prefix** (renamed from `deepseek_proxy_`). Only
  cumulative counters are persisted (`metrics.PERSISTABLE_COUNTERS`); never persist gauges
  or the histogram. Persistence must never crash startup or a request — failures are logged
  and swallowed.

## Conventions

- Match existing style: small module-level functions, `_private` helpers, docstrings that
  explain *why*. No new deps without reason (stdlib first).
- New per-backend behavior usually means a `Provider` field in `config.py` + parsing in
  `_load` + documenting it in `README.md`, `config.example.yaml`, and here.
- Secrets: provider keys inline in `config.yaml` (or `${ENV}`); proxy auth keys via
  `PROXY_API_KEYS`. Never hard-code keys in `app/`. `config.yaml` and `.env` are not
  committed; keep `config.example.yaml` / `.env.example` in sync.
- Logging: human-readable lines (incl. uvicorn) use `<iso-ts> LEVEL <msg>` via the
  `llm-proxy` logger; timestamps are local-time ISO-8601 with offset (set `TZ`, image
  ships tzdata). One structured logfmt line per request (`event=request …`) goes to the
  prefix-free `llm-proxy.event` logger so Loki parses it with `| logfmt`. Caller
  identification lives in `app/clientinfo.py`.

## Testing

No formal suite yet. Validate changes by importing with a config and exercising the
async functions directly, e.g.:

```bash
CONFIG_PATH=config.example.yaml python -c "from app import main; print('imports OK')"
```

For routing/slots/auth, drive the functions with mock backends (local `http.server`)
and mock `registry.provider_model_ids` / `registry._cached_live` for discovery. Cover:
priority admission, queue-when-full, cancellation (no slot leak), failover, the 401 gate,
and streaming slot release. Prefer adding a real `tests/` suite (pytest + pytest-asyncio)
if you extend behavior substantially.

## Gotchas

- Model ids can contain `:` (e.g. `local.qwen-medium:low`). `provider:model` splits on the
  **first** `:` only, and only treats the prefix as explicit if it matches a known provider.
- `priority` lower = preferred; defaults to config order.
- `queue_timeout: 0` means wait forever (the current default).
- Live discovery is cached per backend for `cache_ttl`; a down backend caches an empty list
  for the full ttl (no hammering) and silently rejoins on recovery.
- `/v1/models` hides ids that are targets of a logical model (clients use the stable logical
  name). A logical model is always listed regardless of backend liveness, so the catalog
  stays stable as backends come and go.
