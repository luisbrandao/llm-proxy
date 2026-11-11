# AGENTS.md

Guidance for AI agents working in this repo: what each part does, how a request moves
through it, and the properties that must not break. Read it before changing routing,
concurrency, auth, or anything that persists.

Three documents, three jobs:

- **`README.md`** is the operator's reference: config fields, environment variables,
  endpoints, metrics, log fields, console tabs.
- **This file** is the map of the code and the list of invariants.
- **Each module's docstring** holds the full reasoning for that module's design. Read it
  before editing the module; it is longer and more specific than anything here.

## What this is

An OpenAI-compatible reverse proxy (FastAPI + httpx, Python 3.11) in front of several
LLM backends. A client sends a clean model name. The proxy resolves it to one or more
prioritized backends, gates them by permission, admits the request to a per-backend
concurrency slot (queueing when all are full), forwards it, fails over on error, and
records what it cost. One process, async, **one uvicorn worker**.

It serves real clients continuously (Open WebUI, coding agents) and callers with no
proxy key, who may only ever reach the open backends. When you touch routing, the auth
gate or error responses, check the keyless path as well as the authorized one: an error
a keyless caller receives has to make sense from the client side alone.

## Commands

```bash
pip install -r requirements-dev.txt
pytest                                   # ~500 tests, no network, ~4 s

# import check against the example config (CI runs this too)
CONFIG_PATH=config.example.yaml python -c "from app import main; print('imports OK')"

# run locally
CONFIG_PATH=config.yaml uvicorn app.main:app --port 8000
```

## Module map

| File | Responsibility |
|---|---|
| `app/main.py` | ASGI entry. Logging setup (everything to stdout, uvicorn's loggers re-pointed), the lifespan (ledger open and close, the config watcher, the `smart` / `rates` / `notify` background tasks, pool shutdown), the public routes (`/health`, `/metrics`, `/logging`, `/v1/models`), the `/admin/*` API on the `admin` router, the `/ui` static mount, and the catch-all that calls `proxy.proxy_request`. `_provider_view`, `_serialize_targets` and `_config_snapshot` are the only serializers the admin views use. |
| `app/proxy.py` | The request lifecycle: `proxy_request` (parse, open the in-flight entry) → `_route` (resolve, gate, filter, order, trim) → `_dispatch` (acquire a slot, `_build_body`, forward, fail over) → `_handle_non_stream` / `_handle_stream`. Also `_emit_request_log` (the `event=request` line and the ledger row), `_apply_pricing`, `_record_metrics`, `_error` (the one OpenAI-shaped error envelope), `_decompress`, `_relay_headers`. |
| `app/router.py` | `resolve(model) -> [Target]`: alias → `provider:model` → `models:` logical model → auto-group. `_allow_listed` runs only with `auto_group: false`. Returns `[]` when nothing serves the model. |
| `app/registry.py` | Live model discovery per backend (cached for `cache_ttl`, single-flight, last good catalog kept for `STALE_GRACE`), backend health (`mark_down` / `is_down` / `clear_down`), and `list_models` for `/v1/models`. |
| `app/slots.py` | Per-backend concurrency: `acquire(targets, timeout, on_skip)`, `release(provider, model)`, `poke()`. Priority admission with round-robin inside a tie tier, then an explicit queue of `_Waiter` futures reordered by model affinity. `in_use` / `resident_model` / `queue_depth` for introspection. |
| `app/smart.py` | `order(targets, asked)`: the cost ranking (when `routing.smart_routing` is on) and the `first` / `last` overrides (always). `price_for` picks quota → published rate → observed price. `refresh()` snapshots the ledger into `_prices` / `_shares` every 60 s. `snapshot()` backs the Routing tab. |
| `app/rates.py` | Background reads of what the vendors publish: price catalogs hourly, subscription quota and balances every 2 minutes (30 s while a subscription is past 95% used). The request path only reads: `card_for`, `quota_state`, `out_of_funds`, `expected_per_mtok`. `accounts_snapshot()` backs `/admin/accounts`. |
| `app/providers/` | One module per vendor (`openrouter`, `nanogpt`, `deepseek`), each with the fetchers it has an API for (`fetch_rates`, `fetch_included`, `fetch_quota`, `fetch_balance`), pure `parse_*` functions, and optionally `adapt_request(payload)`. `__init__` holds `RateCard`, `Override`, `Quota`, `Balance`, `get_vendor`. Endpoints and units are in `docs/providers/`. |
| `app/notify.py` | Warnings to ntfy: balance low or empty, allowance used up, payment refused, topped up. `evaluate()` → `run_once()` → `loop()`. `payment_required()` is the one call from the request path. |
| `app/trim.py` | The context guardrail. `trim_request(payload, body_str, asked, request_id)` returns a `Trimmed` (the shrunk payload plus the numbers) or `None`. `_TokenMeter` counts with a real tokenizer; `_CharMeter` is the fallback. |
| `app/claude_cli.py` | The `kind: claude-cli` backend: an `httpx.AsyncBaseTransport` that answers `POST …/chat/completions` by running the `claude` binary headless. `translate` (OpenAI request → prompt), `_Watch` (catches emulated function calls mid-stream), `_coerce`, `client()`. |
| `app/usage.py` | `parse(response) -> Usage \| None`: every vendor's spelling of tokens, cache reads and writes, reasoning and cost, folded into one object. `format_cost` for the log. Imports nothing from the app. |
| `app/ledger.py` | The cost ledger: one SQLite row per completed request. `record(...)` (never raises), `summary(...)` for `/admin/costs`, `observed_prices` / `observed_shares` for `smart.refresh`, `startup()` / `close()`. |
| `app/inflight.py` | The request feed behind the In-flight tab: `begin()`, `Entry.wait / run / record / finish / cancel`, a bounded history ring, a separately bounded body store, `snapshot()`. |
| `app/configwrite.py` | Console edits persisted to `CONFIG_PATH` through a ruamel round-trip: `persist_model_priorities`, `set_enabled_models`, `set_model_map`, `set_aliases`, `set_logical_model`, `delete_logical_model`, `set_smart`. Each returns `(ok, reason)`. Also `config_writable()` and `config_detached()`. |
| `app/config.py` | Loads `config.yaml` and the environment into dataclasses (`Provider`, `Target`, `LogicalModel`, `Routing`, `Trim`, `Notify`, `Pricing` / `Rate` / `Peak`). Globals `PROVIDERS`, `PROVIDERS_BY_NAME`, `ALIASES`, `LOGICAL_MODELS`, `ROUTING`, `AUTH_KEYS`, `TRIM`, `NOTIFY`, rebound by `reload_if_changed()`. Helpers `native_for`, `vendor_of`, `strip_prefix`. |
| `app/auth.py` | `is_authorized(request)` (a bearer key in `AUTH_KEYS`; no keys configured means the gate is off) and `restricted(provider)`. |
| `app/upstream.py` | The three shared `httpx.AsyncClient`s and their timeouts: `forward_client` (600 s read, 5 s connect), `probe_client` (5 s, 3 s connect), `account_client` (30 s, 5 s connect). |
| `app/clientinfo.py` | Who called, for the log: `client_ip`, `client_host` (cached reverse DNS, off the loop, time-bounded), `service_from_ua`. |
| `app/metrics.py` | The Prometheus series, all prefixed `llm_proxy_`. |
| `app/logbuffer.py` | Ring buffer of the last 2000 log lines, sequence-stamped, for `/admin/logs`. |
| `app/version.py` | Build identity from `APP_VERSION` / `APP_REVISION`; `dev` outside a CI image. Never hot-reloaded. |
| `app/static/` | The web console: `index.html`, `app.css`, `app.js`. Vanilla, no build step, served at `/ui/`. `app.js` is one file split by `/* ── Section ── */` banners (Auth, Tabs, Logging, Models, In-flight, Config, Routing, Costs, Init); grep for `^/\* ──` to navigate. |

Outside `app/`:

| Path | What |
|---|---|
| `tests/` | The pytest suite. See [Testing](#testing). |
| `docs/providers/` | What each vendor's account API returns, with units and what was verified live. |
| `config.example.yaml` | The annotated example config. CI imports the app against it, so it must stay loadable. |
| `Dockerfile` | Bakes the pinned `claude` CLI and the Qwen tokenizer. Build args `APP_VERSION`, `APP_REVISION`, `CLAUDE_CODE_VERSION`. |
| `docker-compose.yml` | The dev compose: file-mounted config, published on port 9999. Production's compose lives in the deploy repo. |
| `.github/workflows/docker-build.yml` | Tests, then image build and push, then the `master-N` git tag. Does not run for pushes that touch only `.md` files. |
| `.claude/skills/deploy-llm-proxy/` | The deploy procedure. See [Deploy](#deploy). |
| `grafana-queue-dashboard.json` | An importable Grafana dashboard for the queue-affinity metrics. |

## Request lifecycle

Any path that is not a route declared in `main.py` reaches `proxy.proxy_request`.

1. **Parse** the body once and read `model` and `stream`. Open the in-flight entry
   (`inflight.begin`).
2. **No model** (the body is not a JSON object, or has no `model`): passthrough. The
   request is forwarded untouched to the first `http` provider the caller is permitted to
   use, with no slot and no failover, and logged as `event=passthrough`. No permitted
   provider → 401.
3. **Resolve**: `router.resolve(model)` returns priority-ordered targets. Empty → 404
   `model_not_found`.
4. **Gate**: an unauthenticated caller loses every `require_permission` target. None
   left → 401.
5. **Filter**: `funded or healthy or targets`. `healthy` drops backends marked down;
   `funded` further drops backends that are `rates.out_of_funds`. A filtered-out backend
   is not tried at all while another can serve, and comes back only when nothing else is
   left.
6. **Order**: `smart.order(candidates, model)` applies the cost ranking when it is
   switched on and the `first` / `last` overrides always. It only permutes the list.
7. **Trim**: `trim.trim_request`, in a worker thread. It runs once, before any target is
   chosen, so every failover attempt forwards the same body.
8. **Dispatch** (`_dispatch`), looping over the candidates that remain:
   `slots.acquire` → `_build_body` (native model id, the vendor's `adapt_request`,
   `provider_routing`, `stream_options.include_usage`, `strip_fields`) → handler.
   It fails over on an `httpx.RequestError`, or on a response whose status is in
   `routing.failover_statuses` while another target remains: release the slot,
   `mark_down`, drop that provider, loop. A 402 also calls `notify.payment_required`.
   When the candidates run out, connection failures become `_backend_error` (502, or 504
   for a timeout) and an upstream error is relayed verbatim with its real status and
   body. `clear_down` runs only on a status below 400.
9. **After the response is delivered** (a background task, or the stream generator's
   `finally`): the handler's single `usage.parse` result, priced by `_apply_pricing` when
   the backend reported no cost, feeds `_record_metrics`, `Entry.record` and
   `_emit_request_log`, which writes the `event=request` line and the ledger row. Only the
   attempt that answered the client reaches this step, so a request is one line and one
   row however many backends it tried.

Errors the proxy generates itself go through `proxy._error`: 401 `unauthorized`,
404 `model_not_found`, 502 `upstream_unavailable`, 504 `upstream_timeout`,
503 `slot_timeout`, 503 `cancelled`. The one exception is a plain-text 503 when no
providers are configured at all.

## Layering

Imports only point downward, so there are no cycles.

```
main                               routes, the /admin router, the lifespan
 └─ proxy                          request lifecycle, failover
     ├─ router → registry          name → targets; discovery and health
     ├─ smart → ledger, rates      ordering, from the snapshots
     ├─ notify → rates             warnings
     ├─ rates → providers/         vendor account reads
     └─ slots · inflight · trim · claude_cli · auth · clientinfo · ledger
 main also uses: configwrite · logbuffer
 leaves: config · upstream · usage · metrics · version
```

`config.py` is the one module everything depends on, and it is read **per operation**
(`conf.X`), never snapshotted at import. That is the whole mechanism behind hot reload:
`reload_if_changed()` rebinds the module globals with no `await` between the rebinds, so
a request sees either the old config or the new one and never a mix.

## Where state lives

Every piece of state that gates admission is a plain dict in one process's memory. That
is what makes the slot handoff correct without a lock, and it is why the single worker is
load-bearing and why a restart is a real reset.

| Tier | What | Why |
|---|---|---|
| **Dropped on every config reload** | `registry._cache`, `registry._last_good` | A changed `base_url` points somewhere else, so a catalog from the previous endpoint is worse than none. |
| **Survives a reload, lost on restart** | `slots._in_use` / `_running` / `_last_model` / `_waiters` / `_rr`, `registry._down_until`, `inflight._active` / `_recent` / `_bodies`, `logbuffer`, `smart._prices` / `_shares`, `rates._cards` / `_included` / `_quotas` / `_balances`, `notify._sent` / `_seeded` / `_payment_at`, `clientinfo._dns_cache`, the loaded tokenizer (`trim._loaded`), the Prometheus counters | Keyed by provider *name*, so a request holding a slot still holds it after an edit. |
| **Durable, outside the process** | `config.yaml` (written in place by the console), the cost ledger (`ledger.sqlite`), Loki (one `event=request` line per request), Prometheus (scraped counters) | The durable copy of a request is its log line and its ledger row, which is why the in-memory history may be lossy. |

If you add process-local state, add it to `reset_state` in `tests/conftest.py`, or it
leaks between tests.

## Naming model

Clients only ever see and send **canonical** names. **Native** (provider-specific) ids
never leak.

- `provider.model_map` is keyed by the **native** id (`{native: canonical}`).
  `to_canonical(native)` drives the `/v1/models` listing; `to_native(canonical)` rewrites
  the id on the wire. It must be a bijection per provider: the reverse lookup is built
  once in `__post_init__`, and a collision makes one native id unreachable.
- `enabled_models` is the allow-list, in **native** ids. Empty means "everything the
  backend live-reports".
- A `models:` entry (a logical model, called a "group" in the console) lives in
  canonical space. A target's `model` is the native id; omitted, it is `None` and means
  "inherit `provider.to_native(logical_name)`".
- **`config.native_for(model_name, provider_name, model)` is the single place that
  inheritance is resolved.** Routing, the routing view, the config editor and
  `configwrite` all call it. Do not re-derive it.
- `router.resolve` returns targets whose `model` is already the native wire id.
  `provider_routing` and `pricing` are keyed by that native id.
- `registry.list_models` hides anything a logical model fronts, both by canonical name
  and by the concrete `(provider, native)` of each target.
- The request log carries three names. `model=` is the native id and must stay that way
  (dashboards key on it). `asked=` is what the client sent, omitted when equal to
  `model`; it is the field to group by, because one logical model resolves to a
  different native id per backend. `served=` is what the backend's response says ran,
  emitted only when it differs; it is a diagnostic, never a dashboard key.

## Invariants

### Process and config

- **Single worker.** Slot, queue and health state are in-process. Never add
  `--workers > 1` without moving that state to a shared store.
- **Read config as `conf.X` at use time.** Never `from app.config import PROVIDERS`
  (a snapshot that goes stale after a reload) and never cache config values across
  requests. Importing the dataclasses and helpers (`Provider`, `Target`, `native_for`) is
  fine. After a reload, both the watcher and `main._apply_config_change` (run after a
  console edit) call `registry.clear_cache()` and `slots.poke()`. A file that fails to
  parse leaves the running config in place.
- **Async primitives are created lazily** so they bind to uvicorn's running loop:
  `registry._lock_for`, each `slots._Waiter.future`, `configwrite._lock_for`, the clients
  in `upstream` and `claude_cli`. Do not move them to module-level construction.
- **Outbound calls use the shared clients in `upstream.py`.** Never build a client per
  request and never `aclose()` one from a request path; a stream returns its connection
  by exiting `stream_cm`. A stale pooled connection raises `RemoteProtocolError`, a
  `RequestError`, which the failover path already handles.
- **The forwarding timeout is long-read, short-connect, and both halves matter.** The
  600 s read covers a model loading or a backend that buffers the whole completion; do
  not shorten it. The 5 s connect is there because a bare `Timeout(600)` also sets
  connect to 600 s, and a host that black-holes SYN then holds a slot for ten minutes
  with `down_backoff` never engaging.

### Admission (`slots.py`)

- **Every acquired slot is released exactly once.** Non-stream: in `_dispatch`, after the
  handler returns. Stream: in the generator's `finally`, through the `on_complete`
  callback, because the handler returns before streaming finishes. A new code path must
  guarantee release on every exit, including errors and client disconnect.
- **Admission is a synchronous handoff and needs no lock.** A release decrements, then
  `_drain()` picks the best waiter and resolves its future, all in one synchronous step.
  That is what stops a newly arrived request from barging past the queue. **Never put an
  `await` inside `_drain`, `_release` or `_choose`.** Two consequences to preserve:
  `acquire`'s `finally` hands back a slot granted in the instant it gave up
  (`w.granted and not w.taken`), and `release` must be passed the **native model** that
  finished, or affinity silently degrades to FIFO.
- **Affinity only reorders waiting requests.** It never changes which targets a request
  is eligible for, never holds a slot idle for a better match, and is not consulted on
  the fast path. `affinity_max_skips` is checked *before* affinity in `_choose`; that
  order is the only thing stopping a hot model from starving a cold one.
- **Cancellation must not strand a slot.** The Kill button cancels the task serving a
  request, so `CancelledError` can surface at any `await` in the request path.
  `_dispatch` releases the slot in an `except asyncio.CancelledError` branch. Any new
  `await` that holds a slot needs the same treatment. `Entry.cancel` prefers
  `Entry.stream_task` (bound inside the SSE generator) over the request task, because
  cancelling the request task mid-stream unwinds through Starlette's disconnect listener
  and uvicorn logs a traceback for a deliberate action.

### Routing

- **Never substitute a backend the client did not ask for.** `router.resolve` returning
  `[]` becomes a 404 (`proxy._model_not_found`), never a fallback to `PROVIDERS[0]`. That
  fallback once sent a keyless caller's local-model request to a gated backend during a
  discovery blip, and the caller got a 401 about a model they never asked for. The
  passthrough path follows the same rule: the first backend the *caller is permitted to
  use*, not the first configured one.
- **Auth gate consistency.** Any new model-listing or routing path applies the same
  `require_permission` filtering as `registry.list_models` and `proxy._route`.
- **Discovery failure is not an empty catalog.** `registry._cached_live` keeps the last
  successful catalog for `STALE_GRACE` (300 s) when a probe fails, re-probing every
  `STALE_RETRY` (15 s), because a backend that is swapping models misses its probe
  window routinely. Only a backend with no good catalog in that window caches `[]`.
- **Smart routing only reorders, and never moves what it cannot price.** `smart.order`
  permutes the list `_route` already built, so it cannot add a backend the caller may
  not reach, and failover still walks everything left. A target is rankable only with
  mode `auto` and a price from `price_for`. An unrankable one (unknown price, `fixed`, a
  `claude-cli` seat) keeps its position: an unknown price must never read as cheap.
  Rankable targets are dealt back into the positions rankable ones held, **each position
  keeping its original `priority` value**, so a tie tier is still a tie tier for
  `slots._pick_free`. Do not rewrite priorities to 1..n in the ranking. The `first` /
  `last` override is the exception and does assign strict priorities, because a forced
  backend tied with another would still be round-robined. Work on copies
  (`dataclasses.replace`): the targets are shared with the live config.
- **`backend_modes` values are validated at load and a typo raises.** A mode silently
  read as `auto` would hand the ranking an override the operator believes is in force.
- **A routing label says what moved the order, not what exists.** `order()` reports
  `pinned` only when the override changed the sequence or broke a priority tie, `smart`
  only when the ranking changed the sequence, `smart+pinned` for both, and `None` when
  nothing moved.
- **An empty balance is routing too.** `_route` filters `rates.out_of_funds` backends
  like down ones, keeping them as a last resort because a balance is only as fresh as
  the last read. `402` is a default failover status, so an account that empties between
  two reads moves the request on.

### Forwarding

- **Handlers raise, the dispatcher decides.** `_handle_non_stream` and `_handle_stream`
  let `httpx.RequestError` propagate and return a buffered `Response` carrying the
  upstream status for HTTP errors. `_dispatch` owns both failover triggers and is the
  only place that turns errors into terminal client responses.
- **Streaming fails over only before the first byte.** `_handle_stream` pre-flights the
  connection (`stream_cm.__aenter__`) so `_dispatch` can try the next target before a
  `StreamingResponse` commits its 200. An upstream 4xx/5xx on a stream is buffered and
  returned as a plain response. Do not move error handling inside the body generator.
- **`_build_body` runs per target and never mutates the shared payload.** A vendor's
  `adapt_request` is copy-on-write, so a failover to another vendor still gets the
  client's own history.
- **Decompression handles exactly what it advertises.** `_build_headers` caps the
  forwarded `Accept-Encoding` at `gzip, deflate` and `_decompress` implements those two
  from the stdlib. To add an encoding you need all three: the library in
  `requirements.txt`, the branch in `_decompress`, and the encoding in `_build_headers`.

### Observability

- **The in-flight registry has exactly one closer per entry.** `proxy_request` closes
  the entry for every response except a `StreamingResponse`; that one is closed by the
  stream generator's `finally`, in the leading synchronous block before any `await`.
  `record()` comes before `finish()`, or the history row loses its status and tokens.
  `finish()` is idempotent. Keep the registry write-only from the request path: nothing
  in admission, routing or failover may read it. `Entry.cancel`, driven from the admin
  API, is the one deliberate exception.
- **Body capture is bounded and never leaves the process.** `_bodies` is capped per side
  (`INFLIGHT_BODY_LIMIT`) and evicted with the history. It stays out of `snapshot()`
  (polled every second) and out of stdout, which is what separates it from `LOG_INPUT` /
  `LOG_OUTPUT`.
- **One usage object, four readers.** A handler calls `usage.parse` once and that result
  feeds the metrics, the in-flight row, the log line and the ledger, so they cannot
  disagree. `Usage.cost` is `None` when nothing was reported and `0.0` when the backend
  said zero; never collapse the two.
- **Never persist or re-seed the Prometheus counters.** A restart resetting them to zero
  is a real counter reset that `rate()` and `increase()` handle. A snapshot restored from
  disk lags the last scrape, so the counter comes back lower than Prometheus already
  read, and Prometheus credits the whole pre-drop value as new increase.
  `METRICS_PERSIST` did this and was removed. Long-range totals belong in a recording
  rule.
- **Metric names keep the `llm_proxy_` prefix**, and `llm_proxy_cost_total` counts USD
  only; another currency still reaches the ledger with its label.

### Cost ledger, rates and warnings

- **The ledger is a record, not a counter.** It is append-only, read by `/admin/costs`,
  and never used to initialise a counter, a slot, a health mark or anything else in
  process state. It is write-only from the request path. Every call runs in
  `asyncio.to_thread` on one connection under `_lock`, and `record` never raises: a full
  disk is a rate-limited warning and a lost row.
- **The one reader outside `/admin/costs` is `smart.refresh`**, a background task that
  copies `ledger.observed_prices` and `observed_shares` into `smart._prices` /
  `_shares`. Requests read only those dicts. A broken or empty ledger leaves the last
  snapshot or none, which means published rates alone or the configured order. Do not
  widen this: nothing else may read the ledger on a request.
- **A price not reported is not zero.** The ledger stores `NULL` versus `0`, counts
  `priced` rows next to every cost sum, and the console shows `—` for unknown. A
  provider's `pricing:` table (`proxy._apply_pricing`) prices a response that carried
  token counts but no cost and marks it `Usage.estimated` (`cost_src=table`). It never
  overrides a reported price, never prices a model it does not list, and never prices a
  response with no usage block. Rates are keyed by **native** id and the peak window is
  UTC. The claude-cli backend reports no cost on purpose: its `total_cost_usd` is a
  list-price estimate of usage drawn from a subscription seat.
- **The ledger schema has no migration.** `SCHEMA` is `CREATE TABLE IF NOT EXISTS`, so a
  new column needs an explicit `ALTER TABLE` for the file that already exists in
  production.
- **Vendor account reads are background-only, never raise, and keep the last good
  data.** `rates.refresh_*` run in the lifespan task through `upstream.account_client()`.
  A vendor that errors leaves its cards, quota and balance as they were and warns once
  per `WARN_EVERY`. Keys go only to their own vendor, never to a public catalog
  (OpenRouter's `/models`), and `accounts_snapshot` carries numbers only.
- **Units are the trap.** Every parser converts to USD per million tokens. OpenRouter
  prices per token, and NanoGPT's `cacheReadInputPer1kTokens` is per thousand whatever
  its `unit` field says. A slip is a silent 1000x.
- **A subscription counts as left only while at least 1% of it remains**
  (`nanogpt.SPENT_BELOW`), never "while `remaining > 0`": a request that does not fit is
  billed and not counted, so the counter can park just under the limit until the reset.
  NanoGPT's `recommendedMode` lags, so it may veto and never grant. A subscription is
  free only for the models it covers (`quota_state` consults the covered list). The
  operator's `pricing:` table beats a fetched card. An override that depends on prompt
  length is dropped, not guessed.
- **Warnings never touch a request and do not repeat themselves.** `notify` reads the
  `rates` snapshot, never a vendor, and every send swallows its errors.
  `payment_required` decides synchronously and sends from a background task. A condition
  is announced when it becomes true, reminded every `reminder_hours`, and followed by a
  recovery message. State is in memory, so the first evaluation that finds account data
  seeds silently: a restart must not re-announce what is already true. A notice counts
  as sent only when ntfy accepted it.

### Admin API and config write-back

- **The admin gate is structural.** Every `/admin/*` route is declared on the `admin`
  `APIRouter`, which carries `Depends(require_admin)`. Declare new admin routes on that
  router, never on `app`. The 403 body is `{"error": "unauthorized"}`; the console and
  scripts read that shape.
- **`api_key` never leaves the process.** `_provider_view` and `_config_snapshot` emit
  `has_api_key`, never the value, and `configwrite` writes key lines back verbatim
  without returning one.
- **Routes declared in `main.py` sit above the catch-all.** That includes
  `/favicon.ico` and `/robots.txt`: without them a browser's automatic favicon request
  falls into the proxy path, 401s, and shows up in the request feed as a failed request
  on every page load. Any other well-known browser path belongs there too.
- **`POST /admin/routing/{model}` mutates live target priorities and must re-sort the
  list**, or the priority-tier `groupby` in `slots._pick_free` breaks.
- **Config write-back round-trips through ruamel, never PyYAML.** The config is a
  hand-annotated, git-tracked file; `yaml.dump` would erase its comments. Use `_yaml()`.
- **Mutate the loaded document in place; never rebuild a node.** ruamel remembers each
  scalar's quote style and each collection's flow or block style, and replacing a
  `CommentedMap` / `CommentedSeq` discards it. Every editor keeps surviving entries
  untouched and writes only genuine additions, removals and value changes. The
  regression test: two identical saves leave the file byte-identical.
- **Abort, don't corrupt.** Every mutation is serialized, re-parsed with plain PyYAML,
  and checked to contain exactly the requested change before the file is opened, with a
  hard guard that the provider list is unchanged. Any surprise returns `(False, reason)`
  with the file untouched. A persist failure is a warning in the response, never a 500.
- **The write is in place** (`r+` and truncate), never write-then-rename, because the
  config may be a single-file bind mount pinned to one inode. `config_detached()`
  (`st_nlink == 0`) catches the opposite accident, a host-side rename that orphans the
  container's copy, and refuses every write until the container is recreated.
- **Preserve inherited target ids through an edit.** The config API reports `model` raw
  and `resolved_model` separately (`_serialize_targets(..., raw=True)`). Collapsing them
  would make the editor write an explicit pin for every inherited target on the first
  save.

### Context guardrail (`trim.py`)

- **A no-op for anything it was not built for.** It acts only on a JSON chat body with a
  `messages` list and an integer `num_ctx`, and only when the count exceeds
  `num_ctx - response_headroom`. Everything else returns `None` and `_route` forwards
  the client's bytes unchanged.
- **When it acts:** system messages are never dropped, an assistant `tool_calls` message
  and its `tool` results are kept or dropped together, and the newest block is always
  sent even when it alone is over budget.
- **It reads `num_ctx` before `_build_body` applies `strip_fields`**, so a target that
  strips the field still gets the trimmed conversation.
- **It runs in a worker thread**, so it must stay free of shared mutable state apart
  from `_loaded`, the tokenizer cache, guarded by `_load_lock`.
- **Never let the count run low.** Under-counting forwards the request this exists to
  stop. A tokenizer that will not load falls back to the chars estimate instead of
  skipping the check, and the markup constants are measured on the real template and
  rounded up.

### claude-cli backend

- **It is a transport.** It plugs in under the forwarding path (`proxy._client_for`), not
  as a branch in `_dispatch`, so slots, failover, metrics, the log, the In-flight row and
  Kill need no special case. Keep the backend contract: an answered error is an HTTP
  status, an unanswered one (spawn failure, timeout) raises `httpx.RequestError`.
- **Lockdown.** The container holds `config.yaml` with every backend's API key, so the
  CLI always runs with `--tools ""`, no MCP, no setting sources, no slash commands, in
  an empty directory. Never add a flag that gives a prompt file, shell or network
  access.
- **No orphan process.** `_run` kills the child on timeout and on cancellation before
  the slot is released.
- **Credentials stay in the CLI.** The proxy runs the unmodified binary and never reads,
  copies or forwards its login (the `/claude` volume).
- **Function calling is emulated, never granted.** A request's `tools` become prompt
  text. The model calls them as tools anyway; `_Watch` catches each call in the
  `stream-json` events and stops the run before the CLI's "no such tool" reaches the
  model, and the call goes back as `tool_calls`. `--json-schema` (`_REPLY`) stays on in
  tools mode because it declares the one tool that switches tool use on; without any
  tool declared the model writes a call out as text and invents the result. `_coerce`
  fixes loosely typed scalars against the client's parameter schema. Never "fix" any of
  this by giving the CLI real tools or an MCP server.
- The passthrough path skips non-`http` kinds. With an empty `enabled_models`, the
  catalog is `config.CLAUDE_CLI_MODELS`.

### Build identity

- **The version string is identical to the image tag.** CI passes
  `APP_VERSION=master-<run_number>` as a build arg, the same value the metadata step
  turns into the image tag and the last step pushes as a git tag. `/health`,
  `llm_proxy_build_info` and the console header report it. If you change how the tag is
  derived, change the build arg and the tag step in the same commit.
- **The `ARG` lines sit after `pip install` in the Dockerfile**, because `APP_VERSION`
  changes on every build and would invalidate the install layer. An undefined `ARG`
  expands to `""`, which is why `version.py` treats blank as absent and reports `dev`.

## Changing things

| To add | Touch |
|---|---|
| A `Provider` field | The dataclass and `_load` in `config.py`; `main._provider_view` if the console shows it; the provider table in `README.md`; `config.example.yaml`. |
| A config section | A dataclass and its parsing in `config._load`; the tuple `_load` returns, unpacked in **two** places (the module-level `… = _boot()` and `reload_if_changed`, plus its `global` line); a `conftest` fixture if tests need a neutral default; `README.md`; `config.example.yaml`. |
| An `/admin` endpoint | Declare it on the `admin` router. If `app.js` reads its fields, list them in `tests/test_admin_contract.py`. Add it to the endpoint table in `README.md`. |
| A console config edit | A `configwrite.set_*` built from `mutate(doc)` and `check(parsed)` passed to `_edit`; the endpoint finishes with `_persisted(ok, error, what)`. Test the byte-identical resave. |
| A vendor | `app/providers/<vendor>.py` with the fetchers it has; the name in `config.VENDORS` and its hosts in `_VENDOR_HOSTS`; parser tests against a real payload in `tests/test_providers.py`; a page in `docs/providers/`. |
| Process-local state | A module-level dict keyed by provider name; `conftest.reset_state`; the state table above. |
| A request-log field | The `fields` dict in `proxy._emit_request_log` (`None` values are dropped). To persist it: `ledger.SCHEMA`, `_ROW_COLUMNS`, `_ROW_SQL`, `_insert`, and an `ALTER TABLE` for existing files. Add it to the log table in `README.md`. |
| A metric | `metrics.py`, with the `llm_proxy_` prefix; the metrics table in `README.md`. |

Conventions:

- Small module-level functions, `_private` helpers, docstrings that explain *why*.
  Stdlib first; no new dependency without a reason.
- Provider keys go inline in `config.yaml` or through `${ENV}`; proxy auth keys through
  `PROXY_API_KEYS` or `auth.keys`. Never hard-code a key in `app/`. `config.yaml` is
  gitignored; keep `config.example.yaml` in sync with what the loader accepts.
- Human-readable log lines (uvicorn's included) are `<iso-ts> LEVEL <msg>` on the
  `llm-proxy` logger, with local-time ISO-8601 timestamps that carry the offset. The
  per-request line is pure logfmt on the prefix-free `llm-proxy.event` logger so Loki
  parses it with `| logfmt`. Everything goes to **stdout**.

## Testing

pytest + pytest-asyncio, no network. The suite exists because most of the invariants
above are invisible in review. Each file guards a specific set:

| File | Guards |
|---|---|
| `test_slots.py` | Priority admission, round-robin in a tie tier, queueing, the synchronous handoff (no barging), affinity reordering, the `affinity_max_skips` bound, and no slot stranded by timeout or cancellation. |
| `test_router.py` | Resolution order, inherited versus pinned native ids, an unknown model resolving to `[]` and never to `PROVIDERS[0]`, and `_auto_group` subsuming `_allow_listed`. |
| `test_proxy.py` | The full lifecycle through `httpx.MockTransport`: both failover triggers, error relay, streaming, decompression, the request log, trimming on the wire, smart routing and rate-driven ranking, price tables, empty balances and 402s, the ledger and cost counter. Every test asserts slot occupancy returns to zero. |
| `test_gate.py` | Catalog visibility with and without a key, the 401/404 distinction, admin gating, and `api_key` never appearing in a response. |
| `test_admin_contract.py` | The exact fields `app/static/app.js` dereferences from each admin view. The console is untyped, so a dropped field shows as a blank cell instead of an error. |
| `test_configwrite.py` | Formatting fidelity (comments, key order, quotes, block versus flow), two identical saves leaving the file byte-identical, every refusal path, and `set_smart`. |
| `test_smart.py` | The ranking: inert unless on, cheapest first, positions keep their priority, unpriced / `fixed` / seat backends never moved, equal prices keep the configured order, `first` / `last` strict and unmutating, a mistyped mode failing the load, a failed refresh keeping the last snapshot. |
| `test_rates.py` | The rates service through a mocked `account_client`: the catalog filtered to models in use, the right credentials per vendor, the operator's table beating a fetched card, `quota_state` for every case, a failing vendor keeping its data and warning once. |
| `test_providers.py` | The parsers against payloads copied from the live APIs: unit conversions, time-of-day overrides and their edges, the NanoGPT quota shape, balances, vendor detection from the host. |
| `test_pricing.py` | The `pricing:` table: cached tokens billed once at the cache rate, an unlisted model unpriced unless `"*"` exists, the peak window (weekday, UTC, end-exclusive), a malformed table failing the load. |
| `test_notify.py` | The ntfy warnings through a mocked server: off unless configured, silent seeding, one message per condition, reminders, recovery, a failed send retried, a 402 announced at most hourly; plus `out_of_funds`. |
| `test_ledger.py` | The aggregate shape, the day / range / model / provider filters, retention pruning, a file ledger surviving a reopen, a disabled ledger recording nothing, an unwritable ledger warning once and never raising. |
| `test_usage.py` | The usage parser against real priced samples and every spelling of cache and reasoning counts; no usage block is `None`, `cost: 0` stays a price, malformed fields zero out. |
| `test_inflight.py` | The feed's derived numbers on `Entry` directly: tokens/s over upstream time, reset by a failover, the `~` estimate kept when a backend never reports usage. |
| `test_trim.py` | The guardrail: a no-op unless `num_ctx` is present and exceeded, system messages survive, tool calls and results never separated, excerpts before drops, the newest turn always sent; with a hand-built byte-per-token tokenizer, that the count follows what the template prints. |
| `test_claude_cli.py` | The claude-cli backend through the real subprocess path with a fake CLI script: translation, lockdown flags, a keyless caller never reaching the CLI, CLI errors as HTTP statuses, emulated function calling, and no process outliving its slot. |
| `test_version.py` | The build-identity chain: blank args mean `dev`, a CI build reports its tag and sha, `/health` and `llm_proxy_build_info` carry them. |

What matters when adding tests:

- `tests/conftest.py` sets `CONFIG_PATH` **before** anything imports `app.*`, because
  `config.py` reads it at import time. The fixture config is `tests/fixtures/config.yaml`.
- Autouse fixtures: `reset_state` clears the process-local dicts and points the ledger at
  `:memory:`; `no_smart_refresh_loop` replaces `smart.refresh_loop`, `rates.refresh_loop`
  and `notify.loop` with ones that idle (the real ones would overwrite hand-set snapshots
  and call vendors); `no_notifications` installs an empty `Notify`; `no_baked_tokenizer`
  makes the guardrail count with chars unless a test installs a tokenizer.
- Tests set `smart._prices` and the `rates` dicts directly and call `smart.refresh()`
  themselves.
- `load_config(text)` installs a config through the real reload path; `providers(...)`
  installs `Provider` objects directly.
- Mock upstream with `httpx.MockTransport` and build responses with
  `test_proxy.mock_response`. A plain `httpx.Response(json=...)` arrives with its content
  consumed and `aiter_raw()` raises `StreamConsumed`.
- The Prometheus counters are process-global and not reset between tests. Never assert
  that a series is absent; assert the value a specific request added.
- `test_proxy.py` sets `queue_timeout: 3` instead of the production `0`, so a leaked slot
  fails the suite instead of hanging it. The wait-forever path is covered in
  `test_slots.py`.
- `pytest.ini` turns `DeprecationWarning` into an error. A collection failure in CI that
  names third-party code is dependency drift (requirements are `>=` floors), not the
  change under test.

## Deploy

Production runs the image `ghcr.io/luisbrandao/llm-proxy:master-N` from a compose file in
a separate deploy repo, on port 8000. The config's **directory** is mounted read-write at
`/app/conf` (`CONFIG_PATH=/app/conf/config.yaml`), and the ledger sits beside the config
in that directory. A directory mount, not a file mount: a `git pull` replaces files by
rename, and a file mount would orphan the running container's copy.

The procedure (push, wait for the build, read the real `master-N`, bump the tag, restart,
verify `/health` reports the same tag) is `.claude/skills/deploy-llm-proxy/SKILL.md`. Two
facts that shape day-to-day work:

- A push that touches only `.md` files builds nothing, so there is nothing to deploy.
- Recreating the container drops in-flight requests and resets all in-process state. The
  config and the ledger survive.

## Gotchas

- Model ids can contain `:` (`local.qwen-medium:low`). `provider:model` splits on the
  **first** `:` and treats the prefix as a provider only when it names one. Canonical
  names are colon-free by convention.
- `priority`: lower is preferred. A provider's default is its position in the config; a
  logical target's default is 100.
- `queue_timeout: 0` means wait forever, and is the default.
- `slots` omitted means unlimited: no gating at all for that backend.
- A logical model is listed in `/v1/models` regardless of backend liveness, so the
  catalog stays stable as backends come and go.
- The passthrough path takes no slot and never fails over.
- A failed-over attempt that got an HTTP error still increments
  `llm_proxy_errors_total`, so a request that failed over on a 503 and then succeeded
  counts one error and one request. A connection failure that failed over counts only in
  `llm_proxy_failovers_total`.
- A request killed from the console may lose its `event=request` line: the cancellation
  can interrupt the task that emits it. The kill itself is logged at WARNING.
