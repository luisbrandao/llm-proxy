import asyncio
import logging
import time

import httpx

from app import config as conf

logger = logging.getLogger("llm-proxy")

# Probe timeout for live model discovery. Kept short so a powered-off or
# otherwise unreachable backend fails fast instead of blocking the listing for
# the duration of a long default timeout.
PROBE_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

# provider name -> (expires_at_epoch, [model_id, ...])
_cache = {}

# provider name -> asyncio.Lock, created lazily so each binds to the running
# loop. Coalesces concurrent cold-cache misses into a single upstream probe
# (single-flight) instead of letting every in-flight request fetch in parallel.
_locks = {}


def _lock_for(provider_name: str) -> asyncio.Lock:
    lock = _locks.get(provider_name)
    if lock is None:
        lock = asyncio.Lock()
        _locks[provider_name] = lock
    return lock

# provider name -> epoch until which the backend is considered down. Set when a
# request fails against it so the router can skip it during failover, and
# cleared on the next success.
_down_until = {}


def mark_down(provider_name: str, seconds: float) -> None:
    _down_until[provider_name] = time.time() + seconds


def clear_down(provider_name: str) -> None:
    _down_until.pop(provider_name, None)


def is_down(provider_name: str) -> bool:
    return _down_until.get(provider_name, 0) > time.time()


async def provider_model_ids(provider: conf.Provider):
    """Effective model ids a provider serves: configured list or live-discovered."""
    if provider.lists_all:
        return await _cached_live(provider)
    return list(provider.enabled_models)


async def _fetch_live(provider: conf.Provider):
    url = f"{provider.base_url}/v1/models"
    headers = {}
    if provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return [m["id"] for m in data.get("data", []) if m.get("id")]


async def _cached_live(provider: conf.Provider):
    entry = _cache.get(provider.name)
    if entry and entry[0] > time.time():
        return entry[1]

    # Single-flight: only the first concurrent miss probes; the rest wait here
    # and pick up the fresh cache below, instead of stampeding the backend.
    async with _lock_for(provider.name):
        entry = _cache.get(provider.name)
        if entry and entry[0] > time.time():
            return entry[1]
        try:
            ids = await _fetch_live(provider)
        except Exception as e:
            # Backend is unreachable: drop its models from the listing. Backends
            # are expected to come and go, so this is not an error condition.
            logger.warning(f"Failed to fetch models from {provider.name}: {e}")
            ids = []
        # Stamp expiry AFTER the probe (which may have been slow), so a failed
        # probe is still cached for the full ttl and we don't re-hit a dead
        # backend on every request. Once the box is back, the next miss
        # re-discovers it.
        _cache[provider.name] = (time.time() + provider.cache_ttl, ids)
        return ids


async def list_models(authorized: bool = True) -> dict:
    """Aggregate models, presenting clean server-less names clients can use directly.

    Listed (deduped, in this order): aliases, explicit logical models, and bare
    model ids (a model served by several backends appears once). Backend-prefixed
    `provider:model` ids are not listed — they still work for pinning a specific
    backend, but advertising them would just duplicate the clean names. Offline
    backends drop out.

    When `authorized` is False, backends with `require_permission` are excluded:
    their models are hidden, and shared models are listed with only the open
    backends as owners.
    """
    sep = conf.PROVIDER_SEP
    data = []
    seen = set()

    def visible(provider_name: str) -> bool:
        p = conf.PROVIDERS_BY_NAME.get(provider_name)
        return authorized or not (p and p.require_permission)

    def add(mid: str, owner: str):
        if mid in seen:
            return
        seen.add(mid)
        data.append({"id": mid, "object": "model", "owned_by": owner})

    # Aliases first (e.g. `chat` -> deepseek:deepseek-chat).
    for alias, target in conf.ALIASES.items():
        owner = target.split(sep, 1)[0] if sep in target else "alias"
        if sep in target and not visible(owner):
            continue
        add(alias, owner)

    # Explicit logical models (may span backends with differing real ids). Listed
    # if at least one of its targets is visible to the caller.
    for name, lm in conf.LOGICAL_MODELS.items():
        owners = [t.provider for t in lm.targets if visible(t.provider)]
        if not owners:
            continue
        add(name, ",".join(owners) or "logical")

    # Model ids that are reachable through a logical model. We hide their raw
    # bare ids below: clients should use the stable logical name, and the raw
    # ids (e.g. per-quant variants) would otherwise flap in/out of the list as
    # backends come and go.
    logical_targets = {t.model for lm in conf.LOGICAL_MODELS.values() for t in lm.targets}

    # Probe only the backends visible to this caller, concurrently.
    live_providers = [p for p in conf.PROVIDERS if p.lists_all and visible(p.name)]
    live_results = await asyncio.gather(
        *(_cached_live(p) for p in live_providers),
        return_exceptions=True,
    )
    live_ids = {}
    for provider, result in zip(live_providers, live_results):
        if isinstance(result, Exception):
            logger.warning(f"Model discovery failed for {provider.name}: {result}")
            live_ids[provider.name] = []
        else:
            live_ids[provider.name] = result

    # Group each model id by the visible backends that serve it, skipping ids
    # that a logical model already fronts.
    by_model = {}
    for provider in conf.PROVIDERS:
        if not visible(provider.name):
            continue
        ids = live_ids.get(provider.name, []) if provider.lists_all else provider.enabled_models
        for mid in ids:
            if mid in logical_targets:
                continue
            by_model.setdefault(mid, []).append(provider.name)

    # Each model id once, by its clean server-less name. When several backends
    # serve it, they share the entry (and the proxy load-balances behind it).
    for mid, owners in by_model.items():
        add(mid, ",".join(owners) if len(owners) > 1 else owners[0])

    return {"object": "list", "data": data}
