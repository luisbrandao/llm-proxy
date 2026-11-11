# LLM Proxy

An OpenAI-compatible reverse proxy for **several LLM backends**. Put it between your
clients (Open WebUI, OpenCode, LangChain, any OpenAI SDK) and any number of upstreams
(DeepSeek, OpenRouter, NanoGPT, Google, Ollama hosts). Clients ask for a clean model
name and the proxy decides which backend serves it.

## Highlights

- **Transparent routing.** Clients use a plain model name (`deepseek-v4-flash`,
  `local.qwen-medium:low`) and never need to know which server runs it.
- **Priority and slots.** Prefer one backend over another, cap concurrency per backend,
  and queue requests when every candidate is busy.
- **Swap-aware queueing.** When a local backend frees up, the queue prefers a request
  for the model it already has loaded, so it is not made to swap out a model it is about
  to need again.
- **Failover.** A backend that errors or cannot be reached is skipped and the request
  moves to the next one.
- **Smart routing.** An optional switch that tries a model's backends cheapest-first,
  priced from subscription quota, published rates and observed cost, plus a per-backend
  override to force one first or last.
- **Cost tracking.** What each request cost, by model, backend and day, in a small
  SQLite ledger that survives restarts.
- **Balance and quota awareness.** Reads each vendor's balance and subscription
  allowance, skips an account that is empty, and can warn you on ntfy.
- **Context guardrail.** Shrinks a chat that cannot fit the context size its client
  declared, instead of forwarding it to a paid backend.
- **Permission gate.** Paid backends sit behind a bearer key; open backends work for
  everyone.
- **Claude on your subscription.** A `kind: claude-cli` backend runs the official Claude
  Code CLI headless, so Claude routes like any other backend.
- **Web console.** Live logs, in-flight requests with a Kill button, costs, routing and
  a config editor, at `/ui/`.
- **Observability.** Prometheus metrics and one logfmt line per request.

```
Your app (OpenAI client)
       │  POST /v1/chat/completions   { "model": "deepseek-v4-flash" }
       ▼
┌──────────────────────────────────────────────┐
│                  llm-proxy                   │
│  resolve model → ordered targets             │
│  auth gate → slot acquire (priority + queue) │
│  forward → fail over on error                │
│  log tokens and cost · export metrics        │
└───┬─────────────┬──────────────┬─────────────┘
    ▼             ▼              ▼
 OpenRouter    DeepSeek     Ollama hosts        (any backend in config.yaml)
```

## Quick start

```bash
cp config.example.yaml config.yaml   # backends, slots, priorities, keys
# set PROXY_API_KEYS in docker-compose.yml
docker compose up -d
```

The bundled compose publishes the proxy on host port **9999**. Point any OpenAI client
at `http://<host>:9999/v1` and request a model by its clean name. Send
`Authorization: Bearer <key>` to reach gated backends. The console is at
`http://<host>:9999/ui/`.

The examples below use port 8000, the port the proxy listens on inside the container.

## How routing works

### Names: canonical and native

A **canonical name** is what clients send (`deepseek-v4-pro`). A **native id** is what
one backend calls that model on the wire (`deepseek/deepseek-v4-pro-0813:thinking` on
NanoGPT, `deepseek/deepseek-v4-pro-0813` on OpenRouter). Clients only ever see and send
canonical names.

Two config layers produce canonical names, and they compose without overlap:

- **`model_map`** (per provider) translates between native and canonical for one
  backend. It is keyed by the native id and must be one-to-one: two native ids sharing
  a canonical name would make one of them unreachable.
- **`models:`** (global) is the routing table: one canonical name in front of several
  prioritized targets. A target's native id is its explicit `model:`, or, when omitted,
  inherited from that provider's `model_map`.

`models:` decides which backends serve a name and in what order. `model_map` decides
what each backend calls it.

```yaml
providers:
  - name: nanoGPT
    model_map:
      "deepseek/deepseek-v4-pro-0813:thinking": deepseek-v4-pro
      "qwen3.8-max:thinking": qwen-max
```

### Resolution order

1. **Alias**: a global short name that expands to `provider:canonical` (`chat` →
   `deepseek:deepseek-v4-pro`).
2. **Explicit `provider:model`**: forces one backend. The model part is canonical and is
   rewritten to that backend's native id.
3. **Logical model**: a `models:` entry.
4. **Auto-group**: the same canonical name served by several backends is load-balanced
   with no config, ordered by each backend's `priority`. With `auto_group: false`, the
   first backend whose `enabled_models` lists the model is used instead.

If nothing matches, the request gets a **404 `model_not_found`**. The proxy never picks
a backend on the client's behalf: a wrong backend produces an error (typically a 401
about a model the caller never asked for) that cannot be diagnosed from the client side.

```json
{"error": {"message": "The model 'x' does not exist or is not available on this proxy",
           "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}
```

### Slots, priority and queueing

Each backend declares `slots` (the maximum number of in-flight requests, shared across
all its models) and `priority` (lower is preferred). A request takes a slot from the
highest-priority candidate that has one free. When several candidates tie on priority,
it round-robins across the tied backends that have a free slot. If all candidates are
full it waits, indefinitely by default or up to `routing.queue_timeout`.

> `glm-4.7-flash` runs on `ollamaCitrine` (priority 1) and `ollamaGW` (priority 2).
> Citrine is preferred until full, then gw; if both are full the request waits. Two
> backends at the same priority would alternate per request.

A released slot is handed directly to a waiting request in the same step as the release,
so a newly arrived request cannot jump the queue.

### Model affinity: the queue reorders to avoid swaps

Local backends (llama-swap, Ollama) hold one model in memory and swap on demand, and a
swap usually costs more than the request itself. Strict FIFO provokes the worst case.
Take a single-slot backend serving `modelA` and `modelB`:

| | queue order served | model loads |
|---|---|---|
| FIFO | `modelA` running → `modelB`, `modelA` | **3**: swap B in, swap A back |
| Affinity | `modelA` running → `modelA`, `modelB` | **2**: A stays, B swaps in once |

So when a slot frees, admission prefers a waiting request whose model that backend
already has loaded. This only reorders waiting requests. It never changes which backends
a request may use and never holds a slot idle hoping for a better match.

Starvation is bounded: a request passed over `routing.affinity_max_skips` times
(default 3) is admitted next regardless of what is loaded. `affinity_max_skips: 0` or
`queue_affinity: false` gives strict FIFO.

`llm_proxy_queue_affinity_grants_total` counts reorderings and
`llm_proxy_queue_starvation_yields_total` counts the times the cap stepped in. The
In-flight tab marks a passed-over row `passed over N×` and shows each backend's loaded
model. For Grafana, import [`grafana-queue-dashboard.json`](grafana-queue-dashboard.json)
or query directly:

```promql
# model loads avoided over the dashboard range
sum(increase(llm_proxy_queue_affinity_grants_total[$__range])) or vector(0)

# how often the starvation cap has to step in
sum(increase(llm_proxy_queue_starvation_yields_total[$__range])) or vector(0)
```

Keep the `or vector(0)`: a labelled counter does not exist until its first increment, so
a panel for a cap that has never fired reads "No data" instead of zero.

### Failover

When the chosen backend fails, the proxy releases the slot, marks the backend down for
`routing.down_backoff` seconds, and tries the next target. A failure is either:

- a **connection-level error** (refused, timed out, reset), or
- an upstream response whose status is in `routing.failover_statuses` (default
  `402, 429, 500, 502, 503, 504`).

Other 4xx responses (bad request, auth) are relayed as they are, since every backend
would reject them the same way. When all candidates have failed, the client gets the
last upstream error verbatim, with its real status and body. A streaming request can
fail over until the first byte; after that the HTTP status is already committed.

A backend marked down is skipped while another candidate is up, and is tried again when
it is the only one left.

Errors the proxy generates itself use the OpenAI error shape:

| Status | `error.type` | When |
|---|---|---|
| 401 | `unauthorized` | The model lives only on gated backends and the caller sent no valid key |
| 404 | `invalid_request_error` (`code: model_not_found`) | No backend serves the model |
| 502 | `upstream_unavailable` | Every candidate failed to connect |
| 504 | `upstream_timeout` | The last candidate timed out |
| 503 | `slot_timeout` | `queue_timeout` passed with no free slot |
| 503 | `cancelled` | The request was killed from the console |

### Smart routing

`priority` is a decision made once, in the config. Smart routing makes it per request:
**cheapest backend first**. It is off by default, and with it off the priority order
runs unchanged. The Routing tab has the switch (written back to
`routing.smart_routing`), or set it in the file.

Each backend that can serve the model is priced, in this order of preference:

1. **A subscription with allowance left is free**, for the models it covers. Once the
   allowance is used up, the same backend is priced at its list price, or ranked last
   when it refuses to bill past the allowance.
2. **The vendor's published rate**, read in the background (see
   [Rates, quota and balances](#rates-quota-and-balances)) and evaluated at the instant
   of the request, so a peak-hour surcharge counts when it applies. It is weighted by
   how requests for this model behave on that backend: the share of prompt tokens its
   cache serves and the share of tokens that are output, both from the
   [ledger](#cost-tracking). An unreliable cache shows up as a higher price, and a rate
   change is felt within the hour with no traffic needed.
3. **What it has been charged**, for a backend with no published rates: dollars per
   million tokens over the last `smart_window_days` (7), trusted only with
   `smart_min_requests` (5) priced requests behind it.

The rules it follows:

- **It only reorders.** The backends a request may reach are the ones resolution
  produced and the auth gate left. Smart routing never adds one, and failover still
  walks the rest.
- **It never moves what it cannot price.** A backend none of the three sources can
  price, or one set to `fixed`, keeps the place its `priority` gives it; only the
  priced ones trade places. An unknown price is never read as cheap. Equal prices keep
  the configured order, and a priority tie stays a tie and still round-robins.
- **Overrides always win.** `routing.backend_modes` sets a mode per backend:

  | mode | meaning |
  |---|---|
  | `auto` (default) | takes part in the ranking when smart routing is on |
  | `fixed` | never moved by the ranking |
  | `first` | tried before every other backend of the model |
  | `last` | tried after every other backend of the model |

  `first` and `last` apply with smart routing off too. They set an order, not
  exclusivity: the other backends stay failover targets. A `claude-cli` backend is
  always `fixed`, since it draws from a subscription seat and has no per-request price.
- **It shows what it did.** A request whose backend order was changed carries a badge
  in the In-flight tab and `route=` in its log line: `smart` (the cost ranking),
  `pinned` (a `first`/`last` setting) or `smart+pinned`. An override that already
  agrees with the order moves nothing and shows nothing. The Routing tab lists each
  model's order now beside its configured one, and the price used for each backend with
  its source (`quota`, `list`, `config`, `observed`).

Known limits: for OpenRouter the catalog price is the cheapest upstream host's, while
the request goes to whichever host OpenRouter picks, so treat it as a lower bound. The
cache and output shares come from recent traffic and are a default until a backend has
some.

### Rates, quota and balances

A background task reads, for each backend whose vendor it recognises:

| every | what | from |
|---|---|---|
| hour | the vendor's price list, filtered to the models you use, and which models a subscription covers | OpenRouter `/models`, NanoGPT `/api/v1/models?detailed=true` |
| 2 minutes (30 seconds past 95% used) | subscription allowance left and its reset time | NanoGPT `/api/subscription/v1/usage` |
| 2 minutes | account balance | OpenRouter `/credits`, NanoGPT `/api/check-balance`, DeepSeek `/user/balance` |

The vendor is worked out from the host of `base_url` (`openrouter.ai`, `nano-gpt.com`,
`api.deepseek.com`); set `vendor:` on a provider only for a host it does not know.
DeepSeek publishes no rate API, so its prices stay in the
[`pricing:` table](#price-tables), which also wins over a fetched price for any model it
lists. Every endpoint, field and unit is written down in
[`docs/providers/`](docs/providers/README.md).

A vendor that fails keeps its last good data and logs one warning per 15 minutes, so an
outage can age the numbers but never empties them or fails a request. The Costs tab
shows each backend's balance and allowance meter, and `GET /admin/accounts` returns the
same.

Two things about NanoGPT's subscription that the proxy accounts for:

- The allowance counts **input tokens only, cached input included**, and covers only
  some models. A model outside the subscription bills the balance even while allowance
  remains.
- The allowance counts as left only while at least 1% of it remains. A request that
  does not fit is billed and not counted, so the counter can stop a little short of the
  limit and stay there until the reset. NanoGPT's own `recommendedMode` verdict lags
  behind the counter, so the proxy lets it veto but never grant.

### Empty balances and warnings

A prepaid backend that runs dry answers `402`. The proxy keeps that away from clients:

- **An empty backend is skipped while another can serve.** A backend whose balance was
  last read as a cent or less, with no subscription allowance to pay for it, is left out
  of the candidates. If every backend looks empty the request is still tried, because a
  balance is only as fresh as the last read.
- **A 402 fails over.** When an account empties between two reads, the request moves to
  the next backend. The last backend's 402 is relayed verbatim if nothing else can
  serve.
- **The Costs tab says so.** An account tile turns amber under the warning level and red
  when empty.

Set `notify:` in the config and the proxy also warns you on [ntfy](https://ntfy.sh): a
balance under its threshold (`low_balance`, default $1, per backend with
`low_balance_by`), a balance empty, a subscription allowance used up, a payment refused
on a live request, and a recovery message when you top up. You get one message when a
condition becomes true and a reminder every `reminder_hours` (12) while it stays true. A
restart does not re-announce what is already true, a message ntfy did not accept is
retried on the next check, and nothing in it can fail or slow a request.

### Moving a conversation between backends

Smart routing and failover can move a conversation from one backend to another, and
OpenAI-compatible APIs are not interchangeable about history. The known break:
DeepSeek's own API rejects (`400 The reasoning_content in the thinking mode must be
passed back`) a conversation whose earlier tool-call turns were written by NanoGPT or
OpenRouter, because they name the model's reasoning `reasoning` and DeepSeek requires
the key `reasoning_content`. The proxy adds the key, carrying the reasoning text over,
on the way to a DeepSeek backend only. Other backends get the client's history
untouched. Details: [`docs/providers/deepseek.md`](docs/providers/deepseek.md).

### Context guardrail

A chat client sends the whole conversation on every turn. When its own history
management fails, a 512 KB chat goes to a paid backend verbatim. The proxy is the last
hop, so it enforces the limit the client itself declares.

It acts only when all of these hold: the body is a JSON chat with a `messages` list, it
carries an integer `num_ctx` (Open WebUI sends the model's configured context size this
way), and the counted size exceeds `num_ctx - response_headroom`. A request that fits,
or has no `num_ctx`, is forwarded untouched, byte for byte. Otherwise, in order:

1. **Old, oversized tool results are cut to an excerpt.** A `role: tool` message deeper
   than `protect_recent` messages from the end and above `max_tool_result_tokens` keeps
   its head and tail around a `…[llm-proxy cut N characters…]…` marker.
2. **The oldest turns are dropped** until the rest fits. System messages are always
   kept. An assistant message that calls tools and the `tool` results answering it go
   together, so the window never starts on a dangling call.
3. **The newest turn is always sent**, even when it alone is over budget. The backend's
   own error is then the right answer, and the log says so.

Sizes are counted with a real tokenizer: `trim.tokenizer`, a Hugging Face
`tokenizer.json`. The image bakes in Qwen's at `/app/tokenizers/qwen3.8.json`. Each
message is counted the way the chat template prints it, plus template markup and the
tool schemas, which lands within about 1% on agent transcripts and errs high. The
tokenizer loads on the first request that needs it (about 0.4 s and 120 MB), and
counting runs off the event loop.

A relative `tokenizer` path is resolved against the config file's directory. An empty
value, or a file that will not load, falls back to an estimate: serialized JSON
characters divided by `chars_per_token`. Images count a flat 1000 tokens each.

Every trim shows up in three places with the same numbers: a `CONTEXT TRIM request #N …`
warning in the log, `trimmed=` and `trim_capped=` on the request's log line, and a
`trimmed N msgs` badge on the In-flight row. `llm_proxy_trims_total{model}` counts them.
Turn it off with `trim.enabled: false`.

### Timeouts and connection reuse

Outbound calls go through shared, pooled `httpx` clients, so a request to a remote
backend reuses an established TLS connection. The forwarding timeouts are split on
purpose:

- **read: 600 s.** Generation takes minutes when a large model is loading or a backend
  buffers the whole completion before sending anything.
- **connect: 5 s.** A backend that is down refuses instantly. The only way to wait on
  connect is a host that silently drops packets, and that connection will never
  succeed. Since the request holds a slot while it waits, a long connect timeout would
  let one request stall a single-slot backend for minutes.

### Claude via the Claude Code CLI

A provider with `kind: claude-cli` has no URL. Each request runs the official `claude`
binary headless (`claude -p`) inside the proxy's container, logged in with your own
Claude subscription. Usage counts against that plan's limits, not against an API key.

```yaml
  - name: claude
    kind: claude-cli
    require_permission: true      # the subscription is yours: keep it behind the key
    slots: 1                      # one `claude` process at a time
    enabled_models: []            # [] = the CLI's aliases: fable, opus, sonnet, haiku
    model_map:                    # native (CLI alias) -> canonical
      opus: claude-opus
      sonnet: claude-sonnet
```

Log the CLI in once. The login lives in the `/claude` volume and survives recreates:

```bash
docker exec -it llm-proxy claude auth login --claudeai
```

Until then every request answers 502 `Not logged in`. The binary is baked into the image
at a pinned, checksum-verified version (the `CLAUDE_CODE_VERSION` build arg) with its
self-updater off.

`opus`, `sonnet` and the others are the CLI's rolling aliases: always the newest model
of the family, as of the CLI version in the image. The response's `model` field and the
request log's `served=` name the concrete version that answered.

Slots, failover, metrics, the request log and the console's Kill button behave as for
an HTTP backend. A CLI error with an HTTP status is relayed with that status (a
usage-limit 429 fails over like any 429). Anthropic's 529 becomes 503. A refused CLI
login becomes 502, not a 401 the caller would read as their own key. A binary that will
not start, or runs past the 600 s read timeout, counts as a connection failure.

What it serves, and what it refuses:

- **Text chat completions.** Leading system messages become the system prompt. A single
  user message is sent as it is; a longer conversation is sent as a role-tagged
  transcript. `reasoning_effort` maps to `--effort`.
- **Function calling is emulated.** The request's `tools` are listed in the system
  prompt. When the model calls one, the proxy catches the call in the CLI's event
  stream, stops the run, and returns it as `tool_calls` for the client to run. Loosely
  typed arguments (`"5"` for an integer) are converted to what the function's parameter
  schema asks for. `tool_choice` is honored: `none` offers no functions, a named
  function makes only that one callable, and `required` is asked of the model, since a
  call cannot be forced. `parallel_tool_calls: false` returns at most one call. Every
  request re-sends the conversation and the function list, so a client with many tools
  spends quota on them every turn. Legacy `functions` is refused with a 400.
- **Streaming is synthesized.** The answer arrives as one SSE chunk when it is done.
- **Images and audio are refused with a 400**, not silently dropped.
- **The agent is disarmed.** Every built-in tool, MCP server, settings file and slash
  command is switched off and the process runs in an empty directory, because the
  container also holds `config.yaml` with every other backend's API key.
- Other OpenAI parameters (`temperature`, `max_tokens`) have no CLI equivalent and are
  ignored.

The proxy never touches the CLI's credentials; it only runs the unmodified binary.
Anthropic's terms allow your own subscription in the official CLI and forbid lifting its
token into another client or sharing the account, hence `require_permission: true`.

## Configuration

Routing config lives in `config.yaml` (see [`config.example.yaml`](config.example.yaml)
for the annotated version). Provider API keys go inline or through `${ENV_VAR}`
interpolation. Runtime flags come from the environment.

```yaml
aliases:                          # optional short names -> provider:canonical
  chat: deepseek:deepseek-v4-pro

providers:
  - name: deepseek
    api_key: "sk-..."             # or "${DEEPSEEK_API_KEY}"
    require_permission: true      # paid -> only authenticated callers
    base_url: "https://api.deepseek.com"
    enabled_models: [deepseek-chat, deepseek-reasoner]   # native ids
    slots: 10
    cache_ttl: 3600
    model_map:                    # native -> canonical
      deepseek-chat: deepseek-v4-pro
      deepseek-reasoner: deepseek-v4-flash

  - name: ollamaCitrine
    api_key: ""                   # empty -> no auth header (Ollama)
    base_url: "http://citrine.brandao:11434"
    enabled_models: []            # empty -> expose every model it reports
    slots: 1                      # single GPU box
    priority: 1                   # preferred over gw

  - name: ollamaGW
    base_url: "http://gw.brandao:11434"
    enabled_models: []
    slots: 1
    priority: 2

  - name: openRouter
    api_key: "sk-or-..."
    require_permission: true
    base_url: "https://openrouter.ai/api"
    enabled_models:               # native ids
      - deepseek/deepseek-v4-pro-0813
      - deepseek/deepseek-v4-flash
    slots: 10
    model_map:                    # native -> canonical
      deepseek/deepseek-v4-pro-0813: deepseek-v4-pro
      deepseek/deepseek-v4-flash: deepseek-v4-flash
    provider_routing:             # pin OpenRouter's upstream (native key)
      deepseek/deepseek-v4-flash: [deepseek]

# One canonical name backed by several prioritized targets. Omit `model:` to inherit
# the native id from each provider's model_map; set it to pin a specific native id.
models:
  deepseek-v4-flash:
    targets:
      - {provider: openRouter, priority: 1}
      - {provider: deepseek,   priority: 2}

routing:
  queue_timeout: 0    # seconds to wait for a free slot; 0 = wait forever
  failover: true      # retry the next target on backend error
  auto_group: true    # identical canonical names across backends load-balance
  down_backoff: 15    # seconds a failed backend is skipped before retry
  failover_statuses: [402, 429, 500, 502, 503, 504]

trim:
  enabled: true
  tokenizer: /app/tokenizers/qwen3.8.json  # tokenizer.json to count with; "" = estimate
  chars_per_token: 3.0          # the fallback estimate: JSON chars / this
  response_headroom: 4000       # tokens kept free below num_ctx for the reply
  max_tool_result_tokens: 4000  # old tool results above this are excerpted first
  protect_recent: 10            # the newest N messages are never excerpted
```

### Provider fields

| Field | Description |
|---|---|
| `name` | The backend's key, and the `provider:` routing prefix |
| `kind` | `http` (default): an OpenAI-compatible server at `base_url`. `claude-cli`: the Claude Code CLI run in the container (see [Claude via the Claude Code CLI](#claude-via-the-claude-code-cli)). The URL, path and header fields below apply to `http` only |
| `base_url` | Upstream base URL. Required for `http` |
| `api_key` | Sent upstream as `Authorization: Bearer`. Empty means no auth header (Ollama) |
| `enabled_models` | Allow-list in **native** ids. Empty exposes everything the backend reports (queried live). Non-empty exposes exactly these, with no live query |
| `model_map` | **Native → canonical** dictionary. Drives the `/v1/models` listing and the request rewrite. Must be one-to-one |
| `slots` | Maximum in-flight requests, shared across the backend's models. Omitted means unlimited |
| `priority` | Preference when a model has several backends; lower wins. Defaults to the position in the config. Ties round-robin |
| `require_permission` | `true` puts the backend behind a proxy auth key. Default `false` |
| `cache_ttl` | Seconds the live model list is cached for this backend. Default 60 |
| `strip_path_prefix` | A path segment removed before appending to `base_url`, for backends whose OpenAI-compatible root is not `/v1`. Google Gemini: strip `v1`, with `base_url` ending in `/v1beta/openai` |
| `strip_fields` | Top-level request-body keys dropped before forwarding, for strict backends that 400 on unknown fields (Google rejects the `num_ctx` some clients inject) |
| `provider_routing` | OpenRouter only. Per-model upstream pinning keyed by **native** id (`"*"` for the rest). A list pins strictly; a dict is forwarded verbatim as OpenRouter's `provider` field. A `provider` field sent by the client wins |
| `headers` | Extra headers sent upstream, as defaults: a header the client already sent wins. For attribution the client cannot set, such as OpenRouter's `HTTP-Referer` / `X-Title`. Values support `${ENV}` |
| `pricing` | List prices for a backend whose responses carry no `usage.cost`. See [Price tables](#price-tables) |
| `vendor` | `openrouter`, `nanogpt` or `deepseek`. Normally blank and detected from `base_url`. See [Rates, quota and balances](#rates-quota-and-balances) |

### Other sections

| Section | Keys |
|---|---|
| `aliases` | `name: provider:canonical`. Resolved before everything else, and listed in `/v1/models` |
| `models` | `name: {targets: [{provider, model?, priority}]}`. See [Names](#names-canonical-and-native) |
| `routing` | `queue_timeout`, `failover`, `auto_group`, `down_backoff`, `failover_statuses`, `queue_affinity`, `affinity_max_skips`, and for [smart routing](#smart-routing) `smart_routing`, `smart_window_days`, `smart_min_requests`, `backend_modes` |
| `trim` | `enabled`, `tokenizer`, `chars_per_token`, `response_headroom`, `max_tool_result_tokens`, `protect_recent`. See [Context guardrail](#context-guardrail) |
| `notify` | `ntfy` (the topic URL; `http://ntfy:80/<topic>` inside a docker network), `token` (sent as a Bearer token), `low_balance`, `low_balance_by`, `reminder_hours`. Off until `ntfy` is set. See [Empty balances and warnings](#empty-balances-and-warnings) |
| `auth` | `keys`: proxy auth keys, in addition to `PROXY_API_KEYS`. Values support `${ENV}` |

A typo in `backend_modes`, `vendor`, `kind`, a `pricing:` table or a `notify:` number
fails the load instead of being read as a default.

### Environment variables

Set these in `docker-compose.yml`, not in `config.yaml`:

| Variable | Default | Description |
|---|---|---|
| `PROXY_API_KEYS` | _(empty)_ | Comma-separated proxy auth keys for gated backends. Empty, with no `auth.keys`, turns the gate off |
| `CONFIG_PATH` | `config.yaml` | Path to the YAML config |
| `CONFIG_RELOAD_INTERVAL` | `3` | Seconds between config-file change checks. `0` disables hot reload |
| `PORT` | `8000` | Port the proxy binds inside the container |
| `TZ` | `America/Sao_Paulo` | Timezone for log timestamps and the ledger's days |
| `LOG_INPUT` | `false` | Log the full proxied request (curl-style, auth masked). Can be toggled at runtime through `/logging` |
| `LOG_OUTPUT` | `false` | Log the upstream response (pretty JSON, streams reassembled). Can be toggled at runtime through `/logging` |
| `RESOLVE_CLIENT_HOST` | `true` | Reverse-DNS the caller's IP for the request log (cached, off the event loop, time-bounded) |
| `CLIENT_DNS_TIMEOUT` | `1.0` | Seconds to wait for that lookup before logging the IP alone |
| `TRUST_PROXY_HEADERS` | `true` | Trust `X-Forwarded-For` / `X-Real-IP` for the caller's IP. Set `false` when no proxy sits in front |
| `INFLIGHT_HISTORY` | `200` | Finished requests the In-flight tab keeps below the live ones (in memory, lost on restart) |
| `INFLIGHT_BODIES` | `true` | Keep each request's prompt and reply attached to its In-flight row. This holds prompt text in memory: bounded, never written to disk or stdout, readable only through the gated admin API. `false` keeps the feed metadata-only |
| `INFLIGHT_BODY_LIMIT` | `16384` | Bytes kept per side (request, reasoning, response) before truncating |
| `LEDGER_PATH` | `ledger.sqlite` beside the config | The [cost ledger](#cost-tracking). `""` disables it; `:memory:` keeps it for the life of the process |
| `LEDGER_RETENTION_DAYS` | `365` | Ledger rows older than this are pruned at startup and once a day. `0` keeps everything |

> **Run a single worker.** Slot and queue accounting are in-process, so run one uvicorn
> worker (the default). Several workers would split the accounting and break the
> concurrency caps.

### Hot reload and the config mount

Edits to `config.yaml` are hot-reloaded: the file is checked every
`CONFIG_RELOAD_INTERVAL` seconds and applied live. Requests already in flight finish
under the settings they started with. A broken edit (a YAML typo) is logged and
ignored, and the proxy keeps running on the previous config until the file parses
again.

**Mount the config's directory, not the file.** A single-file bind mount is pinned to
one inode. Anything that replaces the file by rename (`git pull`, vim's default save,
`sed -i`) leaves the container on the old inode: host edits never arrive, and console
saves go into a copy nobody else can see and are lost on the next recreate.

```yaml
    environment:
      - CONFIG_PATH=/app/conf/config.yaml
    volumes:
      - ./conf:/app/conf          # rw: the console writes config edits back
```

With a directory mount the ledger's default path lands in the same directory, so it
persists where the config does.

The bundled dev compose mounts the single file for convenience. With it, edit the file
in place (`nano`, a `>` redirect, `:set backupcopy=yes` in vim). The proxy detects a
detached file: the Config tab shows a red banner and every save is refused until you run
`docker compose up -d --force-recreate llm-proxy`.

## Authentication and permissions

Backends marked `require_permission: true` are gated. A request is authorized when it
carries `Authorization: Bearer <key>` with a key from `PROXY_API_KEYS` or `auth.keys`.

- **With a valid key**, a caller sees and uses every backend.
- **Without a key**, gated backends are hidden from `/v1/models`, and requests to them
  (including explicit `provider:model` pins) return **401**. Open backends work for
  everyone.
- **With no keys configured**, the gate is off and everything is open.

A typical setup shares the local Ollama hosts with a friend (no key) while the paid
backends stay private.

The same keys gate `POST /logging` and every `/admin/*` endpoint.

## Endpoints

| Path | Method | Description |
|---|---|---|
| `/*` | any | The proxy. Routed by the `model` in the request body |
| `/models`, `/v1/models` | `GET` | The model list, in canonical names, honoring the auth gate |
| `/health` | `GET` | Liveness and build identity: `{"status":"ok","version":"master-52","revision":"<sha>","release":true}`. No auth |
| `/metrics` | `GET` | Prometheus metrics. No auth |
| `/logging` | `GET` / `POST` | Read or toggle `log_input` / `log_output` at runtime. `POST` needs a key |
| `/ui/` | `GET` | The web console |
| `/admin/logs` | `GET` | Recent log lines from an in-memory ring buffer (`?since=<seq>&level=<min>`) |
| `/admin/inflight` | `GET` | The request feed: running, queued and recently finished, with per-backend slot occupancy |
| `/admin/inflight/{id}/cancel` | `POST` | Kill one in-flight request. `404` if it already finished |
| `/admin/inflight/{id}/body` | `GET` | The prompt and reply captured for one row (see `INFLIGHT_BODIES`) |
| `/admin/costs` | `GET` | Cost and token totals from the [ledger](#cost-tracking): per day, per model (with each model's split across backends), per backend, and the newest requests. `?days=30`, or `since=YYYY-MM-DD&until=YYYY-MM-DD`; narrowed with `day=`, `model=`, `provider=`; `limit=200` caps the request list |
| `/admin/accounts` | `GET` | Each backend's balance (with `state`: `ok`, `low` or `empty`) and subscription quota, as the vendors last reported them. Numbers only, no keys |
| `/admin/upstream-models` | `GET` | Probes every backend's raw `/v1/models` (`?provider=name` for one) |
| `/admin/routing` | `GET` | The routing graph: backends with live slots and health, logical models and priorities, aliases, the smart-routing state |
| `/admin/routing/{model}` | `POST` | Reorder a logical model's target priorities. Applied live and written to the config |
| `/admin/config` | `GET` | The editable config: backends (no secrets), logical models, aliases, routing |
| `/admin/config/providers/{name}/enabled-models` | `PUT` | Set a backend's allow-list (`[]` allows everything it reports) |
| `/admin/config/providers/{name}/model-map` | `PUT` | Set a backend's `model_map`. Refuses a mapping that is not one-to-one |
| `/admin/config/aliases` | `PUT` | Replace the alias map |
| `/admin/config/models/{name}` | `PUT` / `DELETE` | Create, replace or drop a logical model |
| `/admin/config/smart` | `PUT` | The smart-routing switch and per-backend modes: `{"enabled": bool}` and/or `{"modes": {"google": "fixed"}}` |

`/admin/*` answers `403 {"error": "unauthorized"}` without a valid key and never returns
a provider's `api_key`.

## Web console

A dependency-free dashboard served by the proxy at `/ui/`. Paste a proxy key (kept in
your browser's `localStorage`). The header shows the running build.

- **Logging.** A live log tail with a level filter, a text filter, pause and autoscroll,
  plus the `LOG_INPUT` / `LOG_OUTPUT` runtime toggles.
- **In-flight.** What the proxy is doing now, refreshed every second: live requests on
  top, recently finished ones below.
  - A running row shows its backend, the native model id, token counts and `tok/s`. A
    queued row shows which backends it is waiting on and for how long. A finished row is
    frozen with its status, total time, queue wait, tokens and cost.
  - While a stream runs, `tok out` is the proxy's own count of generation steps, shown
    with a `~`, and is replaced by the backend's number when usage arrives in the final
    chunk. `tok in` is unknown until then.
  - `tok/s` is the request log's `speed_tps`: output tokens over the upstream exchange,
    queue wait excluded.
  - Badges flag what happened on the way through: `attempt 2` (failed over),
    `passed over N×` (queue affinity), `trimmed N msgs` (the
    [context guardrail](#context-guardrail)), `smart` / `pinned` (a reordered backend
    list), `cache hit N` (prompt tokens served from the backend's cache).
  - **Body** expands a row to show the prompt and the reply, with a thinking model's
    reasoning kept apart from its answer. Bodies are fetched per row on demand.
  - **Kill** cancels a live request. See [Killing a request](#killing-a-request).
  - The toolbar shows each backend's `in_use/slots`, amber when full, and the model it
    has loaded.
- **Costs.** What the traffic cost, from the [cost ledger](#cost-tracking).
  - An **accounts** strip: each backend's balance and, for a subscription, an allowance
    meter with the reset countdown (amber from 85%, red when used up).
  - One filter row scopes the whole tab: a range (Today, 7d, 30d, 90d, This month, Last
    month, All, or two dates, with `‹` `›` to step by the window's own length), a model,
    a backend, and a day picked by clicking a column of the chart.
  - A KPI row: total cost and how many requests carried a price, requests and failures,
    tokens in and out, cache share, reasoning share, mean cost per priced request.
  - **By day**: a column chart of cost, requests or tokens. **By model**: each model as
    the client asked for it, with a chip per backend that served it, which answers
    "which route for this model is cheaper". **By backend**: requests, cache share,
    cost, cost per request and per million tokens; all `ollama*` hosts count as one
    `ollama`. **Requests**: the newest requests in range.
- **Models.** The catalog clients see, plus **Probe upstreams**, which queries each
  backend's real `/v1/models` regardless of `enabled_models`.
- **Routing.** The smart-routing switch, a mode per backend, each model's order now
  beside its configured one, and the price used for each backend. Below it, each logical
  model with its prioritized targets and live down/busy badges; reorder with `↑` `↓` or
  by editing the number. Auto-grouped models and aliases are read-only here.
- **Config.** Edit `config.yaml` from the browser. Every save rewrites the file with
  comments preserved and takes effect at once.
  - **Upstream models** per backend: allow everything, or an explicit list picked from
    the backend's probed catalog. A pinned id the backend no longer reports stays
    visible and checked, marked *not in catalog*.
  - **Name mapping** per backend edits its `model_map`.
  - **Model groups** adds, edits and deletes logical models. A target's native id left
    blank inherits from `model_map`, and saving it does not turn it into an explicit
    pin.
  - **Aliases** adds, edits and deletes short names. An alias pointing at an unknown
    backend is refused.
  - Every pinned id is tagged with what a client has to send for it: the group that
    fronts it, the name `model_map` gives it, or nothing when the raw id is the public
    name.
  - API keys are never sent to the browser. Editing secrets and adding or removing whole
    backends stay file-side.

### Killing a request

**Kill** cancels the task serving the request, which runs the ordinary cleanup: the slot
is released, the upstream connection closed, and the request moves into the history as
`cancelled`. It works on a queued request and on one stuck in an upstream read that
will never return.

- **Queued, or a non-streaming response:** the caller gets a `503` with
  `{"error": {"type": "cancelled"}}`.
- **Mid-stream:** the SSE stream ends. The `200` was committed with the first chunk, so
  the client sees a truncated completion.

Each kill is logged at `WARNING` with the request id, model, backend and elapsed time.

### How console edits reach the file

Every console edit rewrites `config.yaml` through a ruamel.yaml round-trip, so comments,
key order, quoting and blank lines survive. The one thing ruamel cannot keep is
hand-aligned padding inside flow maps (`{provider: nanoGPT,      priority: 2}`), which is
normalized once, on the first console write.

- The config must be mounted **read-write**. On a `:ro` mount the edit is refused as
  "not persisted" and the tabs show a read-only warning.
- **Abort, don't corrupt.** Each edit is serialized, re-parsed, and checked to contain
  exactly the requested change before the file is opened for writing. An edit may never
  add or remove a provider. Any surprise aborts with a reason and leaves the file
  byte-identical.
- The write is in place, never write-then-rename, so it works on a single-file bind
  mount.
- The edit is applied to the running process before the response returns.
- `api_key` values are written back verbatim and never leave the process.

## The model list

`/v1/models` lists, deduplicated and in canonical names: aliases, logical models, and
every other model once. A model served by several backends appears a single time.

- Backend-prefixed `provider:model` ids are not listed. They still work for pinning.
- Anything a logical model fronts is hidden, both its canonical name and the native ids
  of its targets, so per-backend variants do not appear and disappear as backends come
  and go. A logical model is always listed.
- Backends with an empty `enabled_models` are queried live (`GET {base_url}/v1/models`),
  cached for `cache_ttl`, with concurrent cold requests sharing one probe.
- **A failed probe does not blank a backend.** A local backend busy loading a model can
  miss its probe window, so the last catalog that succeeded stands in for up to 5
  minutes, re-probing every 15 seconds. Past that, a backend that is still failing drops
  out of the list.
- Gated backends are hidden from callers without a valid key.

## Prometheus metrics

Scrape `http://<host>:8000/metrics`:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `llm_proxy_requests_total` | Counter | `provider`, `model` | Completed requests |
| `llm_proxy_tokens_input_total` | Counter | `provider`, `model` | Input tokens |
| `llm_proxy_tokens_output_total` | Counter | `provider`, `model` | Output tokens |
| `llm_proxy_tokens_cached_total` | Counter | `provider`, `model` | Prompt tokens served from the backend's prompt cache (a subset of input) |
| `llm_proxy_cost_total` | Counter | `provider`, `model` | Cost in USD: what the backend reported, or the [price table](#price-tables) estimate. A backend with neither adds nothing, so this is a floor on spend |
| `llm_proxy_request_duration_seconds` | Histogram | `provider`, `model` | Request latency (0.1 to 600 s) |
| `llm_proxy_errors_total` | Counter | `provider`, `model`, `status_code` | Failed requests |
| `llm_proxy_failovers_total` | Counter | `provider` | Failovers away from a backend |
| `llm_proxy_slots_in_use` | Gauge | `provider` | In-flight requests holding a slot |
| `llm_proxy_queue_waiting` | Gauge | | Requests waiting for a slot |
| `llm_proxy_queue_affinity_grants_total` | Counter | `provider` | Times the queue admitted a request ahead of an earlier one to avoid a model swap |
| `llm_proxy_queue_starvation_yields_total` | Counter | `provider` | Times `affinity_max_skips` forced a passed-over request through |
| `llm_proxy_trims_total` | Counter | `model` | Requests shrunk by the [context guardrail](#context-guardrail), keyed by the name the client sent |
| `llm_proxy_build_info` | Gauge | `version`, `revision` | Always 1; the labels are the running build |

The `model` label is the native id. Requests with no model (passthroughs) are left out
of the per-model metrics.

### Counters reset on restart, on purpose

In-memory counters go back to zero when the proxy restarts. Prometheus recognises that
as a counter reset, so `rate()` and `increase()` stay correct across a reboot.

Do not snapshot the counters to disk and re-seed them on boot. This repo once did
(`METRICS_PERSIST`) and it was removed, because it turns a harmless reset into a
fabricated spike: the snapshot lags the last scrape, the counter comes back lower than
the value Prometheus already read, and Prometheus credits the entire pre-drop value as
new increase.

For long-range totals, use reset-proof `:increase5m` recording rules instead of the raw
counter:

```promql
sum_over_time(llm_proxy_requests_total:increase5m[$__range])
```

## Cost tracking

Some backends say what a request cost. OpenRouter and NanoGPT put the price in the
response's `usage.cost` (USD) and report how much of the prompt was served from a
prompt cache and how many completion tokens were reasoning. DeepSeek reports cache hits
but no price. Google, Copilot and local Ollama report neither. The Claude CLI backend
reports cache reads and writes and deliberately no price, since it draws from a
subscription seat.

The proxy reads all of those spellings into one usage record per request and sends it
to four places:

1. the **request log** line: `cost=`, `cached=`, `cache_write=`, `reasoning=`, each
   present only when the backend reported it;
2. the **In-flight** row's cost cell and `cache hit` badge;
3. the metrics `llm_proxy_cost_total` and `llm_proxy_tokens_cached_total`;
4. the **cost ledger**: one row per completed request in a SQLite file (`LEDGER_PATH`),
   which the console's Costs tab and `GET /admin/costs` read.

The ledger exists because its questions (what did this model cost this month, which
backend served it cheaper) span restarts, and nothing else in the process does. It is an
append-only record, never fed back into a counter. Rows older than
`LEDGER_RETENTION_DAYS` are pruned. A ledger that cannot be written is a warning, never
a failed request.

A price the backend did not report is **unknown, not zero**. The ledger stores `NULL`,
the tab shows `—` and counts how many requests were priced next to every total, so a
total over mixed backends reads as the floor it is. `cost=0` is different: a free model
is priced, at zero.

### Price tables

For a backend that never reports a price, give its provider block a `pricing:` table and
the proxy prices each request from the token counts the response does carry:

```yaml
  - name: deepseek
    base_url: "https://api.deepseek.com"
    pricing:                     # USD per 1M tokens, keyed by NATIVE model id
      models:
        deepseek-v4-flash: {input: 0.15, cached: 0.003, output: 0.60}
      peak:                      # DeepSeek bills 2x on weekday UTC mornings
        multiplier: 2
        weekdays: [mon, tue, wed, thu, fri]
        utc_hours: ["01-04", "06-10"]   # whole UTC hours, end exclusive
```

The price is `(in - cached) × input + cached × cached + out × output`, times the peak
multiplier when the request started inside the window. `cached` defaults to `input`, and
a `"*"` entry covers unlisted models.

Such a price is an estimate at list price, not a charge. It goes to the same four
places as a reported one, and the log line marks it with `cost_src=table`. The rules:

- a price the backend did report always wins;
- a model missing from the table, with no `"*"` entry, stays unpriced;
- a response with no usage block stays unpriced, never zero;
- a malformed table fails the config load.

Vendor holidays (DeepSeek bills Chinese public holidays off-peak) are not modelled, so
on those days the estimate errs high. The same table is what [smart routing](#smart-routing)
uses as the backend's published rate.

## Request logging

Every completed request, success or error, emits one structured
[logfmt](https://brandur.org/logfmt) line on stdout:

```
ts=2026-06-17T02:48:13-03:00 level=info event=request provider=openRouter model=deepseek/deepseek-v4-pro-0813 asked=deepseek-v4-pro status=200 stream=true in=5524 out=890 cached=5000 reasoning=120 cost=0.00037 dur=0:00:26 speed_tps=34.91 client_ip=192.168.1.50 client_host=workstation.lan svc=OpenWebUI ua="OpenWebUI/0.5"
```

| Field | Meaning |
|---|---|
| `ts` | ISO-8601 timestamp with offset (local time per `TZ`) |
| `provider`, `model` | The backend chosen and the **native** model id sent to it |
| `asked` | The model name the client sent, when it differs from `model`. Group by this to chart what people requested: a logical model spanning three backends has three native ids |
| `served` | The model the backend's response says it ran, when it differs from `model`. Records the concrete version behind a rolling alias |
| `status` | The upstream HTTP status relayed to the client |
| `stream` | Whether the response was streamed |
| `in`, `out` | Prompt and completion tokens |
| `dur` | Duration of the upstream exchange as `H:MM:SS`, rounded up to the second |
| `cached`, `cache_write`, `reasoning` | When reported: prompt tokens read from the cache (a subset of `in`), prompt tokens written to it, and completion tokens spent thinking (a subset of `out`) |
| `cost` | USD, as a plain decimal, when the backend reported one or the provider has a [price table](#price-tables). `cost=0` is a free model; no field means nobody said |
| `cost_src` | `table` when `cost` is the proxy's list-price estimate |
| `speed_tps` | Output tokens per second. Omitted for embeddings and rerankers |
| `op`, `in_tps` | On embeddings and rerankers only: `embedding` or `rerank`, and input tokens per second |
| `route` | When smart routing or a `first`/`last` override changed the backend order: `smart`, `pinned` or `smart+pinned` |
| `trimmed`, `trim_capped` | When the [context guardrail](#context-guardrail) acted: messages dropped and tool results cut to an excerpt |
| `client_ip`, `client_host` | The caller's address and its reverse-DNS name |
| `svc`, `ua` | The service guessed from the User-Agent's first token, and the full User-Agent |
| `err` | A short error category (`invalid_request`, `unauthorized`, `not_found`, `rate_limited`, `upstream_error`); absent on success |

Embedding and rerank requests return no completion tokens, so they are tagged `op=` and
report `in_tps` in place of `speed_tps`. Detection is by request path and by model id
(a name containing `embed` or `rerank`).

A request whose body carries no `model` (a non-chat or multipart passthrough, forwarded
untouched to the first HTTP backend the caller may use) is logged as
`event=passthrough`, keyed by `method` and `path`. These stay out of the per-model
metrics and the ledger.

Loki and Grafana parse the line with no regex:

```logql
{container="llm-proxy"} | logfmt | status>=`400`            # all failed requests
{container="llm-proxy"} | logfmt | provider=`openRouter`    # one backend
{container="llm-proxy"} | logfmt | trimmed!=""              # every trimmed request
sum by (asked) (sum_over_time({container="llm-proxy"} | logfmt | unwrap cost [$__range]))   # spend per model
```

The line is emitted after the response is delivered, so the reverse-DNS lookup never
adds latency for the caller. All other log lines (the verbose dumps, warnings, uvicorn's
own) use `<ts> LEVEL <msg>` with the same timestamp format, also on stdout.

### Verbose logging

`LOG_INPUT` logs the full proxied request curl-style, with auth masked. `LOG_OUTPUT`
logs the upstream response pretty-printed; a stream is reassembled into one JSON with
`_assembled_content` and `_reasoning_content`. Both can be flipped at runtime:

```bash
curl http://<host>:8000/logging                          # current state
curl -X POST http://<host>:8000/logging \
  -H "Authorization: Bearer <key>" \
  -d '{"log_input": true, "log_output": true}'           # either key optional
```

Runtime changes are not kept across restarts; the environment variables set the state
at boot.

### Upstream errors

An upstream error is always logged with its response body (WARNING, pretty-printed,
truncated at 4 KB) whatever `LOG_OUTPUT` says, because the body is where the backend
says why:

```
2026-06-11T23:42:22-03:00 WARNING Upstream error 400 from 'google' (model: gemini-2.5-pro):
{
  "error": { "code": 400, "message": "Unknown name 'num_ctx': Cannot find field.", ... }
}
```

The error body and status are relayed to the client verbatim, on streaming requests
too: an upstream 4xx/5xx is returned as a plain JSON response, not wrapped in a `200`
SSE stream.

> **Streaming token counts.** Upstreams emit a `usage` block in a stream only when the
> request sets `stream_options: {"include_usage": true}`. The proxy adds it to streamed
> requests unless the client set it, so the client receives a final usage chunk.

## Build, versioning and CI

A push to `master` runs the tests and then builds the Docker image and pushes it to
GitHub Container Registry, tagged `latest` and `master-<run_number>`. A push that
touches only `.md` files builds nothing. The same `master-N` is pushed as a git tag on
the built commit, so `git checkout master-70` is the code a running version was built
from.

The image tag and the version the process reports are the same string, so you can tell
whether the container answering requests is the build you pinned:

| Where | Looks like |
|---|---|
| Startup log | `llm-proxy master-52 (1e5837a3) starting — 8 providers, 9 logical models, 0 aliases` |
| `GET /health` | `{"status":"ok","version":"master-52","revision":"1e5837a3…","release":true}` |
| `GET /metrics` | `llm_proxy_build_info{version="master-52",revision="1e5837a3…"} 1.0` |
| Console header | a `master-52` chip next to the title; hover for the commit |

Built outside CI (a plain `docker build`, or running from a checkout) the version is
`dev` with `release: false`.

```bash
# does the running process agree with what the compose file pins?
curl -s <host>:8000/health | jq -r .version
```

Stack: Python 3.11, FastAPI and uvicorn, httpx, prometheus-client, PyYAML (config
reads), ruamel.yaml (comment-preserving config writes), tokenizers (the context
guardrail), SQLite from the standard library (the ledger).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

About 500 tests, no network, a few seconds. They pin the properties that are invisible
in review: every acquired slot is released on every exit path, two identical config
saves leave the file byte-identical, an unknown model 404s instead of being routed to
an arbitrary backend, and the fields the console reads from `/admin/*` are all still
there. [`AGENTS.md`](AGENTS.md) describes the code's structure, its invariants, and
what each test file guards.

## License

MIT
