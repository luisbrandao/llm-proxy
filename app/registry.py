import logging
import time

import httpx

from app import config as conf

logger = logging.getLogger("deepseek-proxy")

# provider name -> (expires_at_epoch, [model_id, ...])
_cache = {}


async def _fetch_live(provider: conf.Provider):
    url = f"{provider.base_url}/v1/models"
    headers = {}
    if provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return [m["id"] for m in data.get("data", []) if m.get("id")]


async def _cached_live(provider: conf.Provider):
    now = time.time()
    entry = _cache.get(provider.name)
    if entry and entry[0] > now:
        return entry[1]
    try:
        ids = await _fetch_live(provider)
    except Exception as e:
        logger.warning(f"Failed to fetch models from {provider.name}: {e}")
        ids = entry[1] if entry else []
    _cache[provider.name] = (now + provider.cache_ttl, ids)
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
    for provider in conf.PROVIDERS:
        if provider.lists_all:
            ids = await _cached_live(provider)
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
