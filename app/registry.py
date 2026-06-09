import asyncio
import logging
import time

import httpx

from app import config as conf

logger = logging.getLogger("deepseek-proxy")

# Probe timeout for live model discovery. Kept short so a powered-off or
# otherwise unreachable backend fails fast instead of blocking the listing for
# the duration of a long default timeout.
PROBE_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

# provider name -> (expires_at_epoch, [model_id, ...])
_cache = {}

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
    try:
        ids = await _fetch_live(provider)
    except Exception as e:
        # Backend is unreachable: drop its models from the listing. Backends
        # are expected to come and go, so this is not an error condition.
        logger.warning(f"Failed to fetch models from {provider.name}: {e}")
        ids = []
    # Stamp expiry AFTER the probe (which may have been slow), so a failed
    # probe is still cached for the full ttl and we don't re-hit a dead backend
    # on every request. Once the box is back, the next miss re-discovers it.
    _cache[provider.name] = (time.time() + provider.cache_ttl, ids)
    return ids


async def list_models() -> dict:
    """Aggregate models, presenting clean server-less names clients can use directly.

    Listed (deduped, in this order): aliases, explicit logical models, bare model
    ids (a model served by several backends appears once), and finally the
    explicit `provider:model` ids for manual targeting. Offline backends drop out.
    """
    sep = conf.PROVIDER_SEP
    data = []
    seen = set()

    def add(mid: str, owner: str):
        if mid in seen:
            return
        seen.add(mid)
        data.append({"id": mid, "object": "model", "owned_by": owner})

    # Aliases first (e.g. `chat` -> deepseek:deepseek-chat).
    for alias, target in conf.ALIASES.items():
        owner = target.split(sep, 1)[0] if sep in target else "alias"
        add(alias, owner)

    # Explicit logical models (may span backends with differing real ids).
    for name, lm in conf.LOGICAL_MODELS.items():
        owners = ",".join(t.provider for t in lm.targets)
        add(name, owners or "logical")

    # Probe every live-discovery backend concurrently, so one slow or offline
    # box waits in parallel with the others instead of serializing behind them.
    live_providers = [p for p in conf.PROVIDERS if p.lists_all]
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

    # Group each model id by the backends that serve it.
    by_model = {}
    explicit = []
    for provider in conf.PROVIDERS:
        ids = live_ids.get(provider.name, []) if provider.lists_all else provider.enabled_models
        for mid in ids:
            by_model.setdefault(mid, []).append(provider.name)
            explicit.append((provider.name, mid))

    # Bare id once (auto-balanced when served by several backends).
    for mid, owners in by_model.items():
        add(mid, ",".join(owners) if len(owners) > 1 else owners[0])

    # Explicit `provider:model` for pinning a specific backend.
    for pname, mid in explicit:
        add(f"{pname}{sep}{mid}", pname)

    return {"object": "list", "data": data}
