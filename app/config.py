import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml


CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
PROVIDER_SEP = ":"

_ENV_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value):
    """Expand ${ENV_VAR} references inside string values."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    return value


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


@dataclass
class Routing:
    queue_timeout: float = 0.0   # seconds to wait for a slot; 0 = wait forever
    failover: bool = True        # on backend error, try the next-priority target
    auto_group: bool = True      # group identical model ids across providers
    down_backoff: float = 15.0   # seconds a failed backend is skipped for
    # Upstream HTTP error statuses that trigger failover to the next target
    # (rather than relaying the error to the client). Server errors + rate
    # limiting by default; 4xx like 400/404 are request problems every backend
    # would reject identically, so they're relayed as-is.
    failover_statuses: frozenset = field(
        default_factory=lambda: frozenset({429, 500, 502, 503, 504})
    )


def _load():
    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f) or {}

    # Global aliases: simple name -> "provider:model" target.
    aliases = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}

    providers = []
    for idx, item in enumerate(raw.get("providers", []) or []):
        slots = item.get("slots")
        providers.append(
            Provider(
                name=item["name"],
                base_url=_interpolate(item["base_url"]).rstrip("/"),
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
    fos = r.get("failover_statuses")
    routing = Routing(
        queue_timeout=float(r.get("queue_timeout", 0) or 0),
        failover=bool(r.get("failover", True)),
        auto_group=bool(r.get("auto_group", True)),
        down_backoff=float(r.get("down_backoff", 15)),
        failover_statuses=(
            frozenset(int(s) for s in fos)
            if fos is not None
            else frozenset({429, 500, 502, 503, 504})
        ),
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

    return providers, aliases, logical, routing, auth_keys


PROVIDERS, ALIASES, LOGICAL_MODELS, ROUTING, AUTH_KEYS = _load()
PROVIDERS_BY_NAME = {p.name: p for p in PROVIDERS}


def _flag(key: str, default: str) -> bool:
    return os.environ.get(key, default).lower() == "true"


# Runtime flags come from environment variables (set via docker-compose).
LOG_INPUT = _flag("LOG_INPUT", "false")
LOG_OUTPUT = _flag("LOG_OUTPUT", "false")
PORT = int(os.environ.get("PORT", "8000"))

# Per-request log enrichment: identify the caller. Reverse-DNS is best-effort
# (cached, run off the event loop, time-bounded) — disable it if your resolver
# is slow or lacks PTR records. Proxy headers are trusted by default so a front
# proxy's X-Forwarded-For wins over the immediate peer.
RESOLVE_CLIENT_HOST = _flag("RESOLVE_CLIENT_HOST", "true")
CLIENT_DNS_TIMEOUT = float(os.environ.get("CLIENT_DNS_TIMEOUT", "1.0"))
TRUST_PROXY_HEADERS = _flag("TRUST_PROXY_HEADERS", "true")

# Optional metric persistence: snapshot cumulative counters to disk and restore
# them on boot so Prometheus deltas stay continuous across restarts. The path
# must live on a volume that survives pod recreation to be useful.
METRICS_PERSIST = _flag("METRICS_PERSIST", "false")
METRICS_PERSIST_PATH = os.environ.get("METRICS_PERSIST_PATH", "metrics_state.json")
METRICS_FLUSH_INTERVAL = int(os.environ.get("METRICS_FLUSH_INTERVAL", "30"))
