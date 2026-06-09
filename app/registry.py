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
    """Aggregate models from every provider, prefixed with `provider:`.

    Global aliases are listed first under their simple name so clients can
    pick them directly (e.g. `chat` -> deepseek:deepseek-chat).
    """
    data = []
    for alias, target in conf.ALIASES.items():
        owner = target.split(conf.PROVIDER_SEP, 1)[0] if conf.PROVIDER_SEP in target else "alias"
        data.append({"id": alias, "object": "model", "owned_by": owner})

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

    for provider in conf.PROVIDERS:
        if provider.lists_all:
            ids = live_ids.get(provider.name, [])
        else:
            ids = provider.enabled_models
        for mid in ids:
            data.append(
                {
                    "id": f"{provider.name}{conf.PROVIDER_SEP}{mid}",
                    "object": "model",
                    "owned_by": provider.name,
                }
            )
    return {"object": "list", "data": data}
