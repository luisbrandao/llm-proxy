"""OpenRouter: published rates (per upstream-aware catalog) and credit balance.

Endpoints and units: docs/providers/openrouter.md. In short: `GET /v1/models` is
public and prices every model in **USD per token as strings**; `GET /v1/credits`
returns what was bought and what was used.

The model-level price is *a* price, not necessarily the one a request pays: each model
is served by several upstream hosts that differ in price, quantization and uptime
(`/v1/models/{slug}/endpoints`), and OpenRouter picks among them. The catalog price
matched the cheapest endpoint in a probe, which is why it is a sound lower bound and
why ranking should treat it as an estimate.
"""
from typing import Dict, Optional

from app.config import Rate
from app.providers import (
    Balance, Override, RateCard, day_index, hhmm_to_minutes, to_float,
)

_PER_MILLION = 1e6


def _root(provider) -> str:
    root = provider.base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _override(entry) -> Optional[Override]:
    """One `pricing.overrides` entry. An entry that depends on prompt length cannot be
    evaluated without the request, so it is left out rather than guessed at."""
    if not isinstance(entry, dict) or "min_prompt_tokens" in entry:
        return None
    prices = {}
    for key, field in (("prompt", "input"), ("input_cache_read", "cached"), ("completion", "output")):
        value = to_float(entry.get(key))
        if value is not None:
            prices[field] = value * _PER_MILLION
    if not prices:
        return None
    days = entry.get("utc_days")
    return Override(
        prices=tuple(sorted(prices.items())),
        days=frozenset(i for i in (day_index(d) for d in days) if i is not None) if days else None,
        start=hhmm_to_minutes(entry["utc_start"]) if "utc_start" in entry else None,
        end=hhmm_to_minutes(entry["utc_end"]) if "utc_end" in entry else None,
    )


def _card(pricing) -> Optional[RateCard]:
    if not isinstance(pricing, dict):
        return None
    prompt, completion = to_float(pricing.get("prompt")), to_float(pricing.get("completion"))
    # Negative is OpenRouter's "price depends on the routed model" (its auto router).
    if prompt is None or completion is None or prompt < 0 or completion < 0:
        return None
    cached = to_float(pricing.get("input_cache_read"))
    base = Rate(
        input=prompt * _PER_MILLION,
        cached=(cached if cached is not None and cached >= 0 else prompt) * _PER_MILLION,
        output=completion * _PER_MILLION,
    )
    overrides = tuple(o for o in (_override(x) for x in pricing.get("overrides") or []) if o)
    return RateCard(base=base, overrides=overrides)


def parse_models(payload) -> Dict[str, RateCard]:
    """`GET /v1/models` -> a card per model id (and per `canonical_slug`, since a
    config may use either spelling)."""
    out: Dict[str, RateCard] = {}
    for model in (payload or {}).get("data") or []:
        card = _card(model.get("pricing"))
        if card is None:
            continue
        for key in (model.get("id"), model.get("canonical_slug")):
            if key:
                out[key] = card
    return out


def parse_credits(payload) -> Optional[Balance]:
    data = (payload or {}).get("data") or {}
    total, used = to_float(data.get("total_credits")), to_float(data.get("total_usage"))
    if total is None or used is None:
        return None
    return Balance(currency="USD", amount=total - used)


async def fetch_rates(client, provider) -> Dict[str, RateCard]:
    # Public: no key is sent to a catalog that does not need one.
    resp = await client.get(_root(provider) + "/v1/models", headers={"User-Agent": "llm-proxy"})
    resp.raise_for_status()
    return parse_models(resp.json())


async def fetch_balance(client, provider) -> Optional[Balance]:
    resp = await client.get(
        _root(provider) + "/v1/credits",
        headers={"Authorization": f"Bearer {provider.api_key}", "User-Agent": "llm-proxy"},
    )
    resp.raise_for_status()
    return parse_credits(resp.json())
