import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml


CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
PROVIDER_SEP = ":"

_ENV_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value):
    """Expand ${ENV_VAR} references inside string values."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    return value


# How a provider is reached. `http` is an OpenAI-compatible server at base_url;
# `claude-cli` runs the official `claude` binary headless (app/claude_cli.py),
# authenticated by whatever that CLI is logged in as — no base_url, no api_key.
KIND_HTTP = "http"
KIND_CLAUDE_CLI = "claude-cli"
KINDS = (KIND_HTTP, KIND_CLAUDE_CLI)

# What a `claude-cli` provider "live-reports": the CLI's own model aliases, each
# tracking the newest model of its family. There is no endpoint to ask, so this
# stands in for discovery when enabled_models is empty.
CLAUDE_CLI_MODELS = ("fable", "opus", "sonnet", "haiku")


@dataclass(frozen=True)
class Rate:
    """One model's list price, USD per million tokens."""
    input: float    # prompt tokens NOT served from the prompt cache
    cached: float   # prompt tokens read from the cache (a subset of the prompt)
    output: float   # completion tokens, reasoning included


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Peak:
    """A surcharge window: inside it every rate is multiplied by `multiplier`.

    DeepSeek's API bills double on weekday UTC mornings. Kept in UTC because
    that is how vendors publish it, and so a TZ change on the host cannot
    silently move the window. `hours` are (start, end) whole UTC hours, end
    exclusive.
    """
    multiplier: float = 1.0
    weekdays: frozenset = frozenset(range(5))   # datetime.weekday(): Mon=0
    hours: Tuple[Tuple[int, int], ...] = ()

    def applies(self, when: datetime) -> bool:
        when = when.astimezone(timezone.utc)
        return when.weekday() in self.weekdays and any(s <= when.hour < e for s, e in self.hours)


@dataclass(frozen=True)
class Pricing:
    """A backend's price list, for backends whose responses carry no price.

    `rates` is keyed by the NATIVE model id (what goes on the wire, and what the
    request log calls `model=`); "*" covers unlisted models. A model with no
    rate stays unpriced — an unknown price is never guessed as zero.
    """
    rates: Dict[str, Rate]
    peak: Optional[Peak] = None

    def price(self, model: str, prompt: int, cached: int, completion: int,
              when: datetime) -> Optional[float]:
        rate = self.rates.get(model) or self.rates.get("*")
        if rate is None:
            return None
        cached = min(max(cached, 0), max(prompt, 0))
        cost = ((prompt - cached) * rate.input + cached * rate.cached
                + completion * rate.output) / 1_000_000
        if self.peak is not None and self.peak.applies(when):
            cost *= self.peak.multiplier
        return cost


def _hour_span(value, where: str) -> Tuple[int, int]:
    """`"01-04"` or `[1, 4]` -> (1, 4)."""
    parts = value.split("-") if isinstance(value, str) else value
    try:
        start, end = (int(x) for x in parts)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: hour span {value!r} must look like \"01-04\" or [1, 4]")
    if not 0 <= start < end <= 24:
        raise ValueError(f"{where}: hour span {value!r} must satisfy 0 <= start < end <= 24")
    return start, end


def _vendor(value, name: str) -> str:
    """A provider's explicit `vendor:`, validated. Loud on a typo, like the other
    tables: a vendor silently read as "none" would leave its quota and rates unread
    with nothing to say why."""
    vendor = str(value or "").strip().lower()
    if vendor and vendor not in VENDORS:
        raise ValueError(f"provider {name!r}: vendor {vendor!r} is not one of {', '.join(VENDORS)}")
    return vendor


def _pricing(raw, where: str) -> Optional[Pricing]:
    """Parse a provider's `pricing:` block. Loud on a malformed table: a typo
    that silently prices at zero would be worse than no table at all."""
    if not raw:
        return None
    rates = {}
    for model, r in (raw.get("models") or {}).items():
        try:
            rates[str(model)] = Rate(
                input=float(r["input"]), cached=float(r.get("cached", r["input"])),
                output=float(r["output"]),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{where}: model {model!r} needs numeric input/output (cached optional)")
    peak = None
    p = raw.get("peak")
    if p:
        days = p.get("weekdays", list(WEEKDAYS[:5]))
        try:
            weekdays = frozenset(WEEKDAYS.index(str(d).lower()[:3]) for d in days)
        except ValueError:
            raise ValueError(f"{where}: peak weekdays must be names like mon, tue, …")
        peak = Peak(
            multiplier=float(p.get("multiplier", 1)),
            weekdays=weekdays,
            hours=tuple(_hour_span(h, where) for h in (p.get("utc_hours") or [])),
        )
    return Pricing(rates=rates, peak=peak)


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str = ""
    # Native model ids this backend may serve, in the backend's own vocabulary.
    # Empty => expose every id the backend live-reports (see `lists_all`).
    enabled_models: List[str] = field(default_factory=list)
    # Per-provider native<->canonical dictionary, keyed by the backend's NATIVE
    # id with the clean client-facing canonical name as the value, e.g.
    # `{"deepseek/deepseek-v4-pro": "deepseek-v4-pro"}`. Used both ways:
    #   - listing: native id -> canonical name (`to_canonical`)
    #   - routing: canonical request -> native id on the wire (`to_native`)
    # Must be a bijection per provider so the reverse lookup is unambiguous.
    model_map: Dict[str, str] = field(default_factory=dict)
    # Optional per-model upstream routing (OpenRouter `provider` field). Keyed
    # by resolved NATIVE model id; value is a list of upstream slugs (strict pin)
    # or a dict passed through verbatim. A "*" key applies to unlisted models.
    provider_routing: Dict[str, object] = field(default_factory=dict)
    cache_ttl: int = 60
    # Max concurrent in-flight requests this backend will handle. None means
    # unlimited (no slot gating). Shared across every model the backend serves.
    slots: Optional[int] = None
    # Preference when a logical model can run on several backends. Lower wins.
    # Defaults to the provider's position in the config list.
    priority: int = 100
    # If true, this backend's models are hidden from unauthenticated clients and
    # their requests are rejected with 401. Authentication is a valid proxy key
    # in the `Authorization: Bearer` header (see AUTH_KEYS).
    require_permission: bool = False
    # Path segment to strip from the incoming request before appending to
    # base_url. Lets backends whose OpenAI-compatible root isn't `/v1` work —
    # e.g. Google Gemini lives at `/v1beta/openai/...`, so strip `v1` and set
    # base_url to `.../v1beta/openai`.
    strip_path_prefix: str = ""
    # Top-level request-body fields to drop before forwarding. For strict
    # backends (Google rejects any unknown field with a 400) — e.g. clients that
    # inject Ollama-isms like `num_ctx`. Default: keep everything.
    strip_fields: List[str] = field(default_factory=list)
    # Extra headers to send upstream, applied as defaults (a header the client
    # already sent wins). Use for backend attribution the client can't set
    # itself — e.g. OpenRouter app identity: {"HTTP-Referer": "...", "X-Title": "..."}.
    headers: Dict[str, str] = field(default_factory=dict)
    # One of KINDS. Everything above about URLs, paths and headers applies to
    # `http` only; the routing fields (models, slots, priority, permission) apply
    # to every kind.
    kind: str = KIND_HTTP
    # Price list used only when a response carries no `usage.cost` (DeepSeek's
    # own API). See `Pricing`.
    pricing: Optional[Pricing] = None
    # Which vendor's account API this backend speaks (app/providers/), so the proxy
    # can read its published rates, subscription quota and balance. Normally left
    # blank: it is worked out from `base_url` (see `vendor_of`). Set it only for a
    # backend reached through a host the detection does not know.
    vendor: str = ""
    # Reverse of model_map (canonical name -> native id), built in __post_init__.
    _to_native: Dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        self._to_native = {v: k for k, v in self.model_map.items()}

    @property
    def lists_all(self) -> bool:
        """Empty enabled_models means: expose every model the provider has."""
        return not self.enabled_models

    def to_canonical(self, native: str) -> str:
        """Native backend id -> clean client-facing name (identity if unmapped)."""
        return self.model_map.get(native, native)

    def to_native(self, canonical: str) -> str:
        """Client-facing name -> native backend id on the wire (identity if unmapped)."""
        return self._to_native.get(canonical, canonical)


# Vendors whose account API the proxy can read (app/providers/). Detected from the
# host of `base_url`; a provider can name one explicitly with `vendor:`.
VENDORS = ("openrouter", "nanogpt", "deepseek")
_VENDOR_HOSTS = {
    "openrouter.ai": "openrouter",
    "nano-gpt.com": "nanogpt",
    "api.nano-gpt.com": "nanogpt",
    "api.deepseek.com": "deepseek",
}


def vendor_of(provider) -> str:
    """The vendor module a provider is served by, or "" when it has none.

    Explicit `vendor:` wins; otherwise the host of `base_url`. A backend with no
    known vendor is simply priced from what its responses report, as before."""
    if provider.vendor:
        return provider.vendor
    host = urlparse(provider.base_url or "").hostname or ""
    return _VENDOR_HOSTS.get(host.lower(), "")


def native_for(model_name: str, provider_name: str, model: Optional[str] = None) -> str:
    """The native wire id a logical target resolves to.

    `model` is a `Target.model`: the explicit native id when the config pins one,
    or None meaning "inherit", i.e. reverse-map the logical (canonical) name
    through that provider's model_map.

    This is the **single** home for that rule, and it has to be, because four
    places need to agree on it: routing sends the result on the wire
    (`router._from_logical`), the routing view uses it as a target's stable
    identity, the config editor must tell an inherited id from a pinned one
    (writing a resolved id back would silently turn every inherited target into
    an explicit pin), and `configwrite` has to match a live `Target` — where an
    inherited id is still None — against a file entry that may spell it out.

    Falls back to the bare name for an unknown provider so a half-finished config
    edit degrades to something inspectable rather than raising.
    """
    if model is not None:
        return model
    p = PROVIDERS_BY_NAME.get(provider_name)
    return p.to_native(model_name) if p else model_name


def strip_prefix(provider, path: str) -> str:
    """Drop a backend's `strip_path_prefix` from an incoming path (if present)."""
    p = path.lstrip("/")
    prefix = (provider.strip_path_prefix or "").strip("/")
    if prefix and (p == prefix or p.startswith(prefix + "/")):
        p = p[len(prefix):].lstrip("/")
    return p


@dataclass
class Target:
    """A concrete place a request can run: a real model on a real provider.

    `model` is the NATIVE id sent on the wire. In a `models:` logical target it
    may be None, meaning "inherit from the provider's model_map" (the logical
    name reverse-mapped to native); set it explicitly only to override (e.g. to
    pin a specific quant). In a resolved target it is always concrete.
    """
    provider: str
    model: Optional[str] = None
    priority: int = 100


@dataclass
class LogicalModel:
    """A client-facing model name backed by one or more prioritized targets."""
    name: str
    targets: List[Target] = field(default_factory=list)


# What a backend may do under smart routing (app/smart.py), set per backend in
# `routing.backend_modes`; an unlisted backend is `auto`.
#   auto   takes part in the cost ranking (when smart routing is on)
#   fixed  smart routing never moves it — it stays where `priority` put it
#   first  operator override: tried before every other backend of the model
#   last   operator override: tried after every other backend of the model
# `first`/`last` apply whether or not smart routing is on: they are the
# "use X because I said so" lever, not part of the cost ranking.
BACKEND_MODES = ("auto", "fixed", "first", "last")


@dataclass
class Routing:
    queue_timeout: float = 0.0   # seconds to wait for a slot; 0 = wait forever
    failover: bool = True        # on backend error, try the next-priority target
    auto_group: bool = True      # group identical model ids across providers
    down_backoff: float = 15.0   # seconds a failed backend is skipped for
    # Queue admission: prefer a waiting request whose model the freeing provider
    # already has loaded over strict FIFO, so a local backend isn't made to swap
    # models it is about to need again. See app/slots.py for the reasoning.
    queue_affinity: bool = True
    # How many times affinity may pass over a waiting request before that request
    # is served regardless of what is loaded. Bounds the unfairness; 0 means
    # affinity never applies (strict FIFO).
    affinity_max_skips: int = 3
    # Upstream HTTP error statuses that trigger failover to the next target
    # (rather than relaying the error to the client). Server errors + rate
    # limiting by default; 4xx like 400/404 are request problems every backend
    # would reject identically, so they're relayed as-is.
    failover_statuses: frozenset = field(
        default_factory=lambda: frozenset({429, 500, 502, 503, 504})
    )
    # Cost-aware ordering of one model's backends (app/smart.py). Off by default:
    # the configured `priority` order — which puts the cheaper quota first — is
    # what runs unless this is switched on.
    smart_routing: bool = False
    # How far back the observed prices it ranks by are read, and how many priced
    # requests a (model, backend) pair needs before it is ranked at all.
    smart_window_days: int = 7
    smart_min_requests: int = 5
    # provider name -> one of BACKEND_MODES. See above.
    backend_modes: Dict[str, str] = field(default_factory=dict)


# Where the Dockerfile puts the tokenizer the context guardrail counts with.
DEFAULT_TOKENIZER = "/app/tokenizers/qwen3.8.json"


@dataclass
class Trim:
    """The context-window guardrail (see app/trim.py). Global, not per backend:
    it keys off the `num_ctx` the *client* sends, which is the client's
    statement of the model's context size whatever backend serves it."""
    enabled: bool = True
    # A Hugging Face `tokenizer.json` to count tokens with. The image bakes in
    # Qwen's (every local model is Qwen3.5+, one shared vocabulary); a relative
    # path is taken from the config file's directory. Empty, or a file that will
    # not load, falls back to `chars_per_token`.
    tokenizer: str = field(default_factory=lambda: DEFAULT_TOKENIZER)
    # The fallback estimate = serialized chars / this. 3 over-counts on purpose:
    # trimming a little early beats forwarding the very request this guards
    # against.
    chars_per_token: float = 3.0
    # Tokens kept free below num_ctx for the reply and anything the backend
    # injects. The budget is `num_ctx - response_headroom`.
    response_headroom: int = 4000
    # A `tool` result older than `protect_recent` messages and above this many
    # tokens is cut to a head+tail excerpt before any turn is dropped. 0 disables.
    max_tool_result_tokens: int = 4000
    # The newest N messages are never excerpted; the current answer usually
    # depends on them verbatim.
    protect_recent: int = 10


def _tokenizer_path(value) -> str:
    """`trim.tokenizer` as a path the loader can open: blank/null means none, a
    relative path is resolved against the config file's directory (in the
    deploy, the mounted /app/conf, so a tokenizer can be dropped next to it)."""
    path = str(value or "").strip()
    if path and not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), path)
    return path


def _read_config() -> bytes:
    with open(CONFIG_PATH, "rb") as f:
        return f.read()


def _stat_sig():
    """Cheap change signature of the config file: (mtime_ns, size)."""
    st = os.stat(CONFIG_PATH)
    return (st.st_mtime_ns, st.st_size)


def _load(text: bytes):
    raw = yaml.safe_load(text) or {}

    # Global aliases: simple name -> "provider:model" target.
    aliases = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}

    providers = []
    for idx, item in enumerate(raw.get("providers", []) or []):
        slots = item.get("slots")
        kind = str(item.get("kind") or KIND_HTTP)
        if kind not in KINDS:
            raise ValueError(
                f"provider {item['name']!r}: unknown kind {kind!r} (expected one of {', '.join(KINDS)})"
            )
        # Required for http (a KeyError names the missing field); meaningless for
        # the CLI kind, which has no endpoint.
        base_url = item["base_url"] if kind == KIND_HTTP else item.get("base_url", "")
        providers.append(
            Provider(
                name=item["name"],
                kind=kind,
                base_url=_interpolate(base_url).rstrip("/"),
                api_key=_interpolate(item.get("api_key", "")),
                enabled_models=item.get("enabled_models") or [],
                model_map=item.get("model_map") or {},
                provider_routing=item.get("provider_routing") or {},
                cache_ttl=int(item.get("cache_ttl", 60)),
                slots=(int(slots) if slots is not None else None),
                priority=int(item.get("priority", idx)),
                require_permission=bool(item.get("require_permission", False)),
                strip_path_prefix=str(item.get("strip_path_prefix", "")),
                strip_fields=item.get("strip_fields") or [],
                headers={str(k): _interpolate(str(v)) for k, v in (item.get("headers") or {}).items()},
                pricing=_pricing(item.get("pricing"), f"provider {item['name']!r} pricing"),
                vendor=_vendor(item.get("vendor"), item["name"]),
            )
        )

    # Explicit logical models: client-facing name -> prioritized targets.
    logical = {}
    for name, spec in (raw.get("models") or {}).items():
        targets = []
        for t in (spec.get("targets") or []):
            targets.append(
                Target(
                    provider=t["provider"],
                    model=t.get("model"),  # None => inherit native via model_map
                    priority=int(t.get("priority", 100)),
                )
            )
        targets.sort(key=lambda x: x.priority)
        logical[str(name)] = LogicalModel(name=str(name), targets=targets)

    r = raw.get("routing") or {}
    modes = {}
    for name, mode in (r.get("backend_modes") or {}).items():
        mode = str(mode).strip().lower()
        if mode not in BACKEND_MODES:
            # Loud: a typo that silently left a backend on `auto` would hand an
            # override the operator believes is in force to the cost ranking.
            raise ValueError(
                f"routing.backend_modes[{name!r}]: {mode!r} is not one of {', '.join(BACKEND_MODES)}"
            )
        modes[str(name)] = mode
    fos = r.get("failover_statuses")
    routing = Routing(
        queue_timeout=float(r.get("queue_timeout", 0) or 0),
        failover=bool(r.get("failover", True)),
        auto_group=bool(r.get("auto_group", True)),
        down_backoff=float(r.get("down_backoff", 15)),
        queue_affinity=bool(r.get("queue_affinity", True)),
        affinity_max_skips=max(0, int(r.get("affinity_max_skips", 3))),
        smart_routing=bool(r.get("smart_routing", False)),
        smart_window_days=max(1, int(r.get("smart_window_days", 7))),
        smart_min_requests=max(1, int(r.get("smart_min_requests", 5))),
        backend_modes=modes,
        failover_statuses=(
            frozenset(int(s) for s in fos)
            if fos is not None
            else frozenset({429, 500, 502, 503, 504})
        ),
    )

    tr = raw.get("trim") or {}
    trim = Trim(
        enabled=bool(tr.get("enabled", True)),
        tokenizer=_tokenizer_path(tr.get("tokenizer", DEFAULT_TOKENIZER)),
        chars_per_token=max(0.5, float(tr.get("chars_per_token", 3.0))),
        response_headroom=max(0, int(tr.get("response_headroom", 4000))),
        max_tool_result_tokens=max(0, int(tr.get("max_tool_result_tokens", 4000))),
        protect_recent=max(0, int(tr.get("protect_recent", 10))),
    )

    # Accepted proxy keys gate the `require_permission` backends. Sourced from
    # the PROXY_API_KEYS env var (comma-separated) and/or `auth.keys` in config
    # (which supports ${ENV_VAR} interpolation). Empty => gate disabled.
    auth_keys = set()
    for k in (raw.get("auth") or {}).get("keys") or []:
        v = _interpolate(str(k)).strip()
        if v:
            auth_keys.add(v)
    for k in os.environ.get("PROXY_API_KEYS", "").split(","):
        k = k.strip()
        if k:
            auth_keys.add(k)

    return providers, aliases, logical, routing, auth_keys, trim


def _boot():
    global _last_sig, _loaded_digest
    # Stat before read: if the file changes in between, the next reload tick
    # sees a signature mismatch and re-checks — a wasted read, never a miss.
    _last_sig = _stat_sig()
    text = _read_config()
    _loaded_digest = hashlib.sha256(text).hexdigest()
    return _load(text)


PROVIDERS, ALIASES, LOGICAL_MODELS, ROUTING, AUTH_KEYS, TRIM = _boot()
PROVIDERS_BY_NAME = {p.name: p for p in PROVIDERS}


def reload_if_changed() -> bool:
    """Re-read CONFIG_PATH and swap the live config if its content changed.
    Returns True only when a new config was applied.

    Cheap when idle — one os.stat per call. The file is read only when
    mtime/size move, and re-parsed only when the bytes actually differ (a
    touch or deploy re-sync flips mtime without changing content). The swap
    is a plain rebind of the module globals: every module reads config as
    `conf.X` per operation (never a from-import snapshot), and since there is
    no await between the rebinds, a request can't observe a half-applied
    config. Requests already in flight keep the Provider objects they
    resolved — they finish under the old settings.

    A parse error propagates to the caller with the running config fully
    intact. The failed signature is still recorded, so broken content is
    parsed (and logged by the caller) once, not on every tick; the next real
    edit triggers a retry.
    """
    global PROVIDERS, ALIASES, LOGICAL_MODELS, ROUTING, AUTH_KEYS, TRIM, PROVIDERS_BY_NAME
    global _last_sig, _loaded_digest
    sig = _stat_sig()
    if sig == _last_sig:
        return False
    text = _read_config()
    _last_sig = sig
    digest = hashlib.sha256(text).hexdigest()
    if digest == _loaded_digest:
        return False
    providers, aliases, logical, routing, auth_keys, trim = _load(text)
    PROVIDERS = providers
    ALIASES = aliases
    LOGICAL_MODELS = logical
    ROUTING = routing
    AUTH_KEYS = auth_keys
    TRIM = trim
    PROVIDERS_BY_NAME = {p.name: p for p in providers}
    _loaded_digest = digest
    return True


def _flag(key: str, default: str) -> bool:
    return os.environ.get(key, default).lower() == "true"


# Runtime flags come from environment variables (set via docker-compose).
LOG_INPUT = _flag("LOG_INPUT", "false")
LOG_OUTPUT = _flag("LOG_OUTPUT", "false")
PORT = int(os.environ.get("PORT", "8000"))

# Hot reload: seconds between config-file change checks (0 disables). Polling,
# not inotify — the config is a bind-mounted single file, where host-side edits
# don't reliably raise inotify events inside the container, and a few stats per
# minute cost nothing.
CONFIG_RELOAD_INTERVAL = float(os.environ.get("CONFIG_RELOAD_INTERVAL", "3"))

# Per-request log enrichment: identify the caller. Reverse-DNS is best-effort
# (cached, run off the event loop, time-bounded) — disable it if your resolver
# is slow or lacks PTR records. Proxy headers are trusted by default so a front
# proxy's X-Forwarded-For wins over the immediate peer.
RESOLVE_CLIENT_HOST = _flag("RESOLVE_CLIENT_HOST", "true")
CLIENT_DNS_TIMEOUT = float(os.environ.get("CLIENT_DNS_TIMEOUT", "1.0"))
TRUST_PROXY_HEADERS = _flag("TRUST_PROXY_HEADERS", "true")

# How many finished requests the In-flight tab keeps below the live ones. Purely
# an in-memory ring buffer (like the log buffer) — it is lost on restart by
# design; Loki has the durable copy of the per-request event log.
INFLIGHT_HISTORY = int(os.environ.get("INFLIGHT_HISTORY", "200"))

# Keep each request's prompt and the model's reply attached to its In-flight row,
# so a single request can be read without grepping it out of the log stream.
# **This captures prompt text in memory by default** (bounded, never written to
# disk, gone on restart, and only readable through the auth-gated admin API) —
# set INFLIGHT_BODIES=false to keep the feed metadata-only. Unlike LOG_INPUT /
# LOG_OUTPUT this never reaches stdout, so it stays out of the log shipper.
INFLIGHT_BODIES = _flag("INFLIGHT_BODIES", "true")
# Per-side cap in bytes. A 170 KiB prompt is ordinary for an agentic client, so
# bodies are truncated head-first rather than stored whole.
INFLIGHT_BODY_LIMIT = int(os.environ.get("INFLIGHT_BODY_LIMIT", "16384"))


def _default_ledger_path() -> str:
    """`ledger.sqlite` beside the config file. In the deploy the config's
    directory is the one rw bind mount, so this is where a file survives a
    container recreate without any new volume."""
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), "ledger.sqlite")


# The cost ledger (app/ledger.py): one SQLite row per completed request, so the
# console's Costs tab can answer "by model / by day" across restarts. Unlike the
# Prometheus counters this is never re-seeded into anything — it is an
# append-only record, like the request log. Set to "" to disable, or ":memory:"
# to keep it for the life of the process only.
LEDGER_PATH = os.environ["LEDGER_PATH"] if "LEDGER_PATH" in os.environ else _default_ledger_path()
# Rows older than this many days are pruned at startup and once a day; 0 keeps everything.
LEDGER_RETENTION_DAYS = int(os.environ.get("LEDGER_RETENTION_DAYS", "365"))
