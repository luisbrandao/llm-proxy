import os
import re
from dataclasses import dataclass, field
from typing import Dict, List

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
    cache_ttl: int = 60

    @property
    def lists_all(self) -> bool:
        """Empty enabled_models means: expose every model the provider has."""
        return not self.enabled_models


def _load():
    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f) or {}

    # Global aliases: simple name -> "provider:model" target.
    aliases = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}

    providers = []
    for item in raw.get("providers", []) or []:
        providers.append(
            Provider(
                name=item["name"],
                base_url=_interpolate(item["base_url"]).rstrip("/"),
                api_key=_interpolate(item.get("api_key", "")),
                enabled_models=item.get("enabled_models") or [],
                model_map=item.get("model_map") or {},
                cache_ttl=int(item.get("cache_ttl", 60)),
            )
        )
    return providers, aliases


PROVIDERS, ALIASES = _load()
PROVIDERS_BY_NAME = {p.name: p for p in PROVIDERS}


def _flag(key: str, default: str) -> bool:
    return os.environ.get(key, default).lower() == "true"


# Runtime flags come from environment variables (set via docker-compose).
LOG_INPUT = _flag("LOG_INPUT", "false")
LOG_OUTPUT = _flag("LOG_OUTPUT", "false")
PORT = int(os.environ.get("PORT", "8000"))
