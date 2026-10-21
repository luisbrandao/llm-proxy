"""Published rates, subscription quota and balances, kept current in the background.

Smart routing used to rank backends by what they had *cost* — an average read back
from the ledger. That cannot see a price change until traffic has crossed it, and a
backend that stops getting traffic never gets re-measured. This module reads the
other source: what each vendor **publishes**, which changes without any request being
sent and is cheap to fetch.

    every CATALOG_EVERY (1 h)    each vendor's price catalog, and which models its
                                 subscription covers, filtered to the models in use
    every ACCOUNTS_EVERY (2 min) subscription quota and balances

Everything lands in plain dicts that the request path only *reads* — the same shape as
`smart._prices` — rebound or updated by a background task, never awaited on a request.
A fetch that fails leaves that vendor's last good data and logs one warning per window,
so a vendor outage can age the numbers but never fail a request or empty the snapshot.
A backend with no vendor module is simply absent here and falls back to observed prices.

What the vendors expose, with units and caveats, is in `docs/providers/`. The proxy's
own unit is **USD per million tokens**; each parser converts into it.

## Precedence

A provider's own `pricing:` table in the config is the operator's statement and wins
over a fetched catalog for the models it lists. Anything else comes from the catalog.

## Quota

A subscription is "free" only for the models it covers. NanoGPT's list of covered
models is fetched with the catalog; a model outside it (kimi-k3 at the time of writing)
bills the balance even while allowance remains. When the covered list could not be read
the model is assumed covered: the common case, and the cost of being wrong is that a
backend that is usually first stays first.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from app import config as conf
from app import upstream
from app.providers import Balance, Override, Quota, RateCard, get_vendor

logger = logging.getLogger("llm-proxy")

CATALOG_EVERY = 3600.0
ACCOUNTS_EVERY = 120.0
# While a subscription is nearly spent, "free" can flip to "billed" within a minute, and
# every request sent in the meantime is priced as free. Read it faster then.
ACCOUNTS_EVERY_NEAR_LIMIT = 30.0
NEAR_LIMIT = 0.95
# One warning per (provider, what) per window, whatever the refresh cadence.
WARN_EVERY = 900.0

# (provider, native id) -> RateCard, for models in use. Rebound per provider.
_cards: Dict[Tuple[str, str], RateCard] = {}
# provider -> native ids its subscription covers. Absent = not read (yet).
_included: Dict[str, frozenset] = {}
_quotas: Dict[str, Quota] = {}
_balances: Dict[str, Balance] = {}
_account_at: Dict[str, float] = {}     # provider -> epoch of the last successful account read
_catalog_at: Dict[str, float] = {}
_errors: Dict[Tuple[str, str], str] = {}   # (provider, "rates"|"quota"|"balance") -> last error
_warned: Dict[Tuple[str, str], float] = {}


# ── what a request reads ────────────────────────────────────────────────────

def _config_card(pricing: conf.Pricing, native: str) -> Optional[RateCard]:
    """The operator's `pricing:` table entry as a card, peak window included."""
    rate = pricing.rates.get(native) or pricing.rates.get("*")
    if rate is None:
        return None
    overrides = ()
    peak = pricing.peak
    if peak is not None and peak.multiplier != 1:
        prices = (
            ("cached", rate.cached * peak.multiplier),
            ("input", rate.input * peak.multiplier),
            ("output", rate.output * peak.multiplier),
        )
        overrides = tuple(
            Override(prices=prices, days=frozenset(peak.weekdays), start=s * 60, end=e * 60)
            for s, e in peak.hours
        )
    return RateCard(base=rate, overrides=overrides, source="config")


def card_for(provider: str, native: str) -> Optional[RateCard]:
    """The published price of `native` on `provider`, or None when unknown."""
    p = conf.PROVIDERS_BY_NAME.get(provider)
    if p is not None and p.pricing is not None:
        card = _config_card(p.pricing, native)
        if card is not None:
            return card
    return _cards.get((provider, native))


def quota_for(provider: str) -> Optional[Quota]:
    return _quotas.get(provider)


def quota_state(provider: str, native: Optional[str]) -> Optional[str]:
    """How a request for `native` on `provider` is billed right now.

        None       the provider has no subscription quota we can read
        "free"     covered by the subscription, allowance left
        "billed"   pays from the balance: not covered, or allowance used up with
                   overage allowed
        "refused"  allowance used up and overage not allowed: the backend will say no
    """
    q = _quotas.get(provider)
    if q is None:
        return None
    covered = _included.get(provider)
    if covered is not None and native is not None and native not in covered:
        return "billed"
    if q.available:
        return "free"
    return "billed" if q.allow_overage else "refused"


# A balance at or below this counts as empty: a cent is below what any request costs, and
# an exact zero is not how a prepaid account runs out (it goes a few millicents negative,
# or sits at a remainder no request can use).
EMPTY_BELOW = 0.01


def out_of_funds(provider: str) -> bool:
    """True when a backend has no money to serve a request with, as last read.

    A subscription with allowance left pays for itself whatever the balance says, so it
    is never out of funds. A backend whose balance we cannot read is assumed fine: the
    cost of a wrong "empty" is a working backend skipped until the next read, the cost
    of a wrong "fine" is one request that fails over."""
    b = _balances.get(provider)
    if b is None:
        return False
    q = _quotas.get(provider)
    if q is not None and q.available:
        return False
    return b.amount <= EMPTY_BELOW


def funds_state(provider: str) -> Optional[str]:
    """`empty`, `low` (under the `notify:` threshold) or `ok`; None when unread."""
    b = _balances.get(provider)
    if b is None:
        return None
    if out_of_funds(provider):
        return "empty"
    return "low" if b.amount < conf.NOTIFY.threshold(provider) else "ok"


def expected_per_mtok(card: RateCard, when: datetime, cache_share: float, out_share: float) -> float:
    """Blended USD per million tokens for a request mix.

    `cache_share` is the fraction of prompt tokens read from the cache and `out_share`
    the fraction of all tokens that are completion. Those two are properties of the
    workload and of how well a backend's cache works — they come from the ledger. The
    rates come from `card`, so a price change is felt at once."""
    r = card.at(when)
    prompt_rate = (1.0 - cache_share) * r.input + cache_share * r.cached
    return (1.0 - out_share) * prompt_rate + out_share * r.output


# ── what the background task keeps fresh ────────────────────────────────────

def wanted(provider) -> set:
    """The native ids this provider is used for: its allow-list, its model_map and
    every logical-model target that lands on it."""
    ids = set(provider.enabled_models) | set(provider.model_map)
    for name, lm in conf.LOGICAL_MODELS.items():
        for t in lm.targets:
            if t.provider == provider.name:
                ids.add(conf.native_for(name, t.provider, t.model))
    return ids


def _vendor_providers():
    out = []
    for p in conf.PROVIDERS:
        vendor = conf.vendor_of(p)
        if vendor:
            out.append((p, get_vendor(vendor)))
    return out


def _warn(provider: str, what: str, exc: BaseException) -> None:
    text = f"{type(exc).__name__}: {exc}"
    _errors[(provider, what)] = text
    now = time.monotonic()
    last = _warned.get((provider, what))
    if last is None or now - last >= WARN_EVERY:
        _warned[(provider, what)] = now
        logger.warning("Rates: could not read %s for '%s' (%s) — keeping the last data", what, provider, text)


def _ok(provider: str, what: str) -> None:
    _errors.pop((provider, what), None)


async def _catalog(client, provider, vendor) -> None:
    fetch = getattr(vendor, "fetch_rates", None)
    if fetch is not None:
        try:
            catalog = await fetch(client, provider)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a vendor outage must not reach a request
            _warn(provider.name, "rates", e)
        else:
            want = wanted(provider)
            fresh = {(provider.name, native): catalog[native] for native in want if native in catalog}
            for key in [k for k in _cards if k[0] == provider.name]:
                del _cards[key]
            _cards.update(fresh)
            _catalog_at[provider.name] = time.time()
            _ok(provider.name, "rates")
    included = getattr(vendor, "fetch_included", None)
    if included is not None:
        try:
            _included[provider.name] = frozenset(await included(client, provider))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _warn(provider.name, "rates", e)


async def refresh_catalogs() -> None:
    """Re-read every vendor's price catalog (and covered-model list). Never raises."""
    pairs = _vendor_providers()
    if pairs:
        client = upstream.account_client()
        await asyncio.gather(*(_catalog(client, p, v) for p, v in pairs))


async def _account(client, provider, vendor) -> None:
    got = False
    quota = getattr(vendor, "fetch_quota", None)
    if quota is not None:
        try:
            q = await quota(client, provider)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _warn(provider.name, "quota", e)
        else:
            _ok(provider.name, "quota")
            got = True
            if q is None:
                _quotas.pop(provider.name, None)     # no active subscription any more
            else:
                _quotas[provider.name] = q
    balance = getattr(vendor, "fetch_balance", None)
    if balance is not None:
        try:
            b = await balance(client, provider)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _warn(provider.name, "balance", e)
        else:
            _ok(provider.name, "balance")
            got = True
            if b is not None:
                _balances[provider.name] = b
    if got:
        _account_at[provider.name] = time.time()


async def refresh_accounts() -> None:
    """Re-read subscription quotas and balances. Never raises."""
    pairs = _vendor_providers()
    if pairs:
        client = upstream.account_client()
        await asyncio.gather(*(_account(client, p, v) for p, v in pairs))


def _signature() -> tuple:
    """What the catalog refresh depends on, so a config edit that adds a model or a
    backend is picked up on the next cycle instead of up to an hour later."""
    return tuple(sorted((p.name, p.base_url, conf.vendor_of(p), tuple(sorted(wanted(p)))) for p, _ in _vendor_providers()))


async def cycle(last_catalog: Optional[float], last_signature) -> tuple:
    """One pass of the background task: the catalogs when they are due or the config
    changed what is wanted, then the accounts. Returns the state for the next pass."""
    signature = _signature()
    now = time.monotonic()
    if last_catalog is None or signature != last_signature or now - last_catalog >= CATALOG_EVERY:
        await refresh_catalogs()
        last_catalog, last_signature = now, signature
    await refresh_accounts()
    return last_catalog, last_signature


def next_delay() -> float:
    """Seconds until the accounts are read again: sooner while any subscription is
    within `NEAR_LIMIT` of its allowance, because that is when the answer changes."""
    if any(q.available and q.percent >= NEAR_LIMIT for q in _quotas.values()):
        return ACCOUNTS_EVERY_NEAR_LIMIT
    return ACCOUNTS_EVERY


async def refresh_loop() -> None:
    """The lifespan task: catalogs hourly (and on a config change), accounts every
    couple of minutes, every half minute while a subscription is nearly spent."""
    last_catalog, last_signature = None, None
    while True:
        last_catalog, last_signature = await cycle(last_catalog, last_signature)
        await asyncio.sleep(next_delay())


def accounts_snapshot() -> dict:
    """What the console shows: each backend's balance and quota and how fresh they
    are. Numbers only — a key never appears here."""
    rows = []
    for p, _ in _vendor_providers():
        q, b = _quotas.get(p.name), _balances.get(p.name)
        errors = [f"{what}: {text}" for (name, what), text in _errors.items() if name == p.name]
        if q is None and b is None and not errors:
            continue
        rows.append({
            "name": p.name,
            "vendor": conf.vendor_of(p),
            "balance": {"currency": b.currency, "amount": b.amount} if b else None,
            "quota": {
                "used": q.used, "limit": q.limit, "remaining": q.remaining, "percent": q.percent,
                "reset_at": q.reset_at, "allow_overage": q.allow_overage,
                "available": q.available, "mode": q.mode,
            } if q else None,
            "state": funds_state(p.name),
            "low_threshold": conf.NOTIFY.threshold(p.name),
            "updated_at": _account_at.get(p.name),
            "rates_at": _catalog_at.get(p.name),
            "error": "; ".join(errors) or None,
        })
    return {"accounts": rows, "rates_at": max(_catalog_at.values()) if _catalog_at else None,
            "cards": len(_cards)}


def reset() -> None:
    """Forget everything (tests)."""
    for d in (_cards, _included, _quotas, _balances, _account_at, _catalog_at, _errors, _warned):
        d.clear()
