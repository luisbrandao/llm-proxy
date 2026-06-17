import logging
from typing import List

from app import config as conf
from app import registry

logger = logging.getLogger("llm-proxy")


def _explicit(raw: str):
    """`provider:model` -> a single forced target (manual override / back-compat).

    The `model` part is a canonical name; it is reverse-mapped to the provider's
    native id for the wire.
    """
    sep = conf.PROVIDER_SEP
    if sep in raw:
        prefix, _, rest = raw.partition(sep)
        provider = conf.PROVIDERS_BY_NAME.get(prefix)
        if provider:
            return [conf.Target(provider.name, provider.to_native(rest), provider.priority)]
    return None


def _from_logical(raw: str):
    """An explicit `models:` entry -> its prioritized targets.

    A target's native id is its explicit `model` if set, otherwise the logical
    (canonical) name reverse-mapped through that provider's model_map.
    """
    lm = conf.LOGICAL_MODELS.get(raw)
    if not lm or not lm.targets:
        return None
    out = []
    for t in lm.targets:
        p = conf.PROVIDERS_BY_NAME.get(t.provider)
        if t.model is not None:
            model = t.model
        elif p:
            model = p.to_native(lm.name)
        else:
            model = lm.name
        out.append(conf.Target(t.provider, model, t.priority))
    return out


async def _auto_group(raw: str):
    """Every provider whose catalog includes this canonical model, by priority.

    Matches on the canonical name (native ids translated via model_map) but the
    resolved target carries the provider's native id for the wire.
    """
    targets = []
    for p in conf.PROVIDERS:
        for native in await registry.provider_model_ids(p):
            if p.to_canonical(native) == raw:
                targets.append(conf.Target(p.name, native, p.priority))
                break
    targets.sort(key=lambda t: t.priority)
    return targets


def _fallback(raw: str):
    """First provider whose allow-list serves the canonical model, else the first
    configured provider. The wire id is the provider's native mapping."""
    for p in conf.PROVIDERS:
        native = p.to_native(raw)
        if native in p.enabled_models:
            return [conf.Target(p.name, native, p.priority)]
    if conf.PROVIDERS:
        p = conf.PROVIDERS[0]
        return [conf.Target(p.name, p.to_native(raw), p.priority)]
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
