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
    enabled_models: List[str] = field(default_factory=list)
    model_map: Dict[str, str] = field(default_factory=dict)
    # Optional per-model upstream routing (OpenRouter `provider` field). Keyed
    # by resolved model id; value is a list of upstream slugs (strict pin) or a
    # dict passed through verbatim. A "*" key applies to unlisted models.
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

    @property
    def lists_all(self) -> bool:
        """Empty enabled_models means: expose every model the provider has."""
        return not self.enabled_models


@dataclass
class Target:
    """A concrete place a request can run: a real model on a real provider."""
    provider: str
    model: str
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
                    model=t.get("model", name),
                    priority=int(t.get("priority", 100)),
                )
            )
        targets.sort(key=lambda x: x.priority)
        logical[str(name)] = LogicalModel(name=str(name), targets=targets)

    r = raw.get("routing") or {}
    routing = Routing(
        queue_timeout=float(r.get("queue_timeout", 0) or 0),
        failover=bool(r.get("failover", True)),
        auto_group=bool(r.get("auto_group", True)),
        down_backoff=float(r.get("down_backoff", 15)),
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

# Optional metric persistence: snapshot cumulative counters to disk and restore
# them on boot so Prometheus deltas stay continuous across restarts. The path
# must live on a volume that survives pod recreation to be useful.
METRICS_PERSIST = _flag("METRICS_PERSIST", "false")
METRICS_PERSIST_PATH = os.environ.get("METRICS_PERSIST_PATH", "metrics_state.json")
METRICS_FLUSH_INTERVAL = int(os.environ.get("METRICS_FLUSH_INTERVAL", "30"))
