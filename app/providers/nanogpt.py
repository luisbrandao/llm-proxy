"""NanoGPT: published rates, the subscription allowance, which models it covers, balance.

Endpoints and units: docs/providers/nanogpt.md. The things that are not obvious:

* `pricing.prompt` / `completion` are USD per **million** tokens, but
  `pricing.cacheReadInputPer1kTokens` is per **thousand**, whatever `pricing.unit` says.
* The listed `pricing` already carries NanoGPT's current discount (`effective_pricing`
  shows the list price beside it).
* The subscription counts **input tokens only**, cached input included, per week (and
  per day if one is configured). Past it, `allowOverage` bills the USD balance at the
  listed price.
* The subscription covers only some models. A model outside it bills the balance even
  while allowance remains, so quota may only be treated as "free" for the included ones.
"""
from typing import Dict, Optional, Set

from app.config import Rate
from app.providers import Balance, Quota, RateCard, to_float


def _root(provider) -> str:
    root = provider.base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _auth(provider) -> dict:
    return {"Authorization": f"Bearer {provider.api_key}", "User-Agent": "llm-proxy"}


def parse_models(payload) -> Dict[str, RateCard]:
    out: Dict[str, RateCard] = {}
    for model in (payload or {}).get("data") or []:
        pricing = model.get("pricing")
        if not isinstance(pricing, dict) or not model.get("id"):
            continue
        prompt, completion = to_float(pricing.get("prompt")), to_float(pricing.get("completion"))
        if prompt is None or completion is None:
            continue
        cached_1k = to_float(pricing.get("cacheReadInputPer1kTokens"))
        out[model["id"]] = RateCard(Rate(
            input=prompt,
            cached=cached_1k * 1000 if cached_1k is not None else prompt,
            output=completion,
        ))
    return out


def parse_included(payload) -> Set[str]:
    return {m["id"] for m in (payload or {}).get("data") or [] if isinstance(m, dict) and m.get("id")}


def parse_quota(payload) -> Optional[Quota]:
    """The tightest of the weekly and daily input-token counters, or None when there is
    no active subscription or no counter is reported."""
    if not isinstance(payload, dict) or not payload.get("active"):
        return None
    limits = payload.get("limits") or {}
    counters = []
    for key in ("weeklyInputTokens", "dailyInputTokens"):
        counter = payload.get(key)
        if not isinstance(counter, dict):
            continue
        used, remaining = to_float(counter.get("used")), to_float(counter.get("remaining"))
        limit = to_float(limits.get(key))
        if used is None:
            continue
        if limit is None and remaining is not None:
            limit = used + remaining
        if remaining is None and limit is not None:
            remaining = max(0.0, limit - used)
        if limit is None or remaining is None:
            continue
        reset_ms = to_float(counter.get("resetAt"))
        counters.append((remaining, int(used), int(limit), reset_ms / 1000 if reset_ms else None))
    if not counters:
        return None
    remaining, used, limit, reset_at = min(counters, key=lambda c: c[0])
    mode = (payload.get("routing") or {}).get("recommendedMode")
    # NanoGPT's own verdict when it gives one; "something is left" when it does not.
    available = (mode == "subscription") if mode in ("subscription", "paygo", "unavailable") else remaining > 0
    return Quota(
        used=used, limit=limit, remaining=int(remaining), reset_at=reset_at,
        allow_overage=bool(payload.get("allowOverage")), available=available, mode=mode,
    )


def parse_balance(payload) -> Optional[Balance]:
    amount = to_float((payload or {}).get("usd_balance"))
    return Balance(currency="USD", amount=amount) if amount is not None else None


async def fetch_rates(client, provider) -> Dict[str, RateCard]:
    # With a key, NanoGPT returns this account's own prices.
    resp = await client.get(_root(provider) + "/v1/models?detailed=true", headers=_auth(provider))
    resp.raise_for_status()
    return parse_models(resp.json())


async def fetch_included(client, provider) -> Set[str]:
    resp = await client.get(_root(provider) + "/subscription/v1/models", headers=_auth(provider))
    resp.raise_for_status()
    return parse_included(resp.json())


async def fetch_quota(client, provider) -> Optional[Quota]:
    resp = await client.get(_root(provider) + "/subscription/v1/usage", headers=_auth(provider))
    resp.raise_for_status()
    return parse_quota(resp.json())


async def fetch_balance(client, provider) -> Optional[Balance]:
    # Documented with `x-api-key` only, and as a POST.
    resp = await client.post(
        _root(provider) + "/check-balance",
        headers={"x-api-key": provider.api_key, "User-Agent": "llm-proxy"},
    )
    resp.raise_for_status()
    return parse_balance(resp.json())
