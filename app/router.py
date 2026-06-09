import logging
from typing import List

from app import config as conf
from app import registry

logger = logging.getLogger("llm-proxy")


def _explicit(raw: str):
    """`provider:model` -> a single forced target (manual override / back-compat)."""
    sep = conf.PROVIDER_SEP
    if sep in raw:
        prefix, _, rest = raw.partition(sep)
        provider = conf.PROVIDERS_BY_NAME.get(prefix)
        if provider:
            model = provider.model_map.get(rest, rest)
            return [conf.Target(provider.name, model, provider.priority)]
    return None


def _from_logical(raw: str):
    """An explicit `models:` entry -> its prioritized targets (model_map applied)."""
    lm = conf.LOGICAL_MODELS.get(raw)
    if not lm or not lm.targets:
        return None
    out = []
    for t in lm.targets:
        p = conf.PROVIDERS_BY_NAME.get(t.provider)
        model = p.model_map.get(t.model, t.model) if p else t.model
        out.append(conf.Target(t.provider, model, t.priority))
    return out


async def _auto_group(raw: str):
    """Every provider that serves the bare model id, ordered by provider priority."""
    targets = []
    for p in conf.PROVIDERS:
        ids = await registry.provider_model_ids(p)
        if raw in ids:
            targets.append(conf.Target(p.name, raw, p.priority))
    targets.sort(key=lambda t: t.priority)
    return targets


def _fallback(raw: str):
    """First provider that lists the model, else the first configured provider."""
    for p in conf.PROVIDERS:
        if raw in p.enabled_models:
            return [conf.Target(p.name, p.model_map.get(raw, raw), p.priority)]
    if conf.PROVIDERS:
        p = conf.PROVIDERS[0]
        return [conf.Target(p.name, p.model_map.get(raw, raw), p.priority)]
    return []


async def resolve(raw_model: str) -> List[conf.Target]:
    """Resolve a client-supplied model name into prioritized targets.

    Order: alias expansion -> explicit `provider:model` -> explicit `models:`
    entry -> auto-grouped identical ids -> single-provider fallback.
    """
    raw = conf.ALIASES.get(raw_model, raw_model)

    explicit = _explicit(raw)
    if explicit is not None:
        return explicit

    logical = _from_logical(raw)
    if logical is not None:
        return logical

    if conf.ROUTING.auto_group:
        grouped = await _auto_group(raw)
        if grouped:
            return grouped

    return _fallback(raw)
