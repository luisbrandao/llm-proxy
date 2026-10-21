"""Smart routing: order a model's backends by what a request would cost.

A logical model can run on several backends, and `priority` says which to try
first. That order is a decision made once, in the config — usually "the cheap
quota first". Smart routing is the other way to decide it: **per request, cheapest
backend first**.

## How a backend is priced

For each backend that can serve the model, in this order of preference:

1. **Quota.** A subscription with allowance left, for a model it covers, costs nothing
   (`rates.quota_state`). Once the allowance is used up the same backend is priced at its
   list price — or ranked last if it refuses to bill past it — which is what makes
   "quota first, then decide" happen without anyone flipping a switch.
2. **Published rate.** The vendor's own price list (`rates.card_for`, refreshed hourly
   with no traffic needed), evaluated at *this instant* so a peak-hour surcharge counts
   when it applies, then weighted by how requests for this model actually behave on
   that backend: the share of prompt tokens its cache serves and the share of tokens that
   are output, from the ledger. A rate change is therefore felt within the hour, and a
   backend nobody has sent anything to lately is still priced, so none can get stuck.
3. **Observed price.** For a backend with no published rates, what it has actually been
   charged per million tokens over `smart_window_days`, from the ledger — kept as the
   fallback, and only trusted with `smart_min_requests` priced requests behind it.

A backend that none of these can price is *unrankable* and keeps its place.

Rates answer "what does a token cost now"; the ledger answers "how does this workload use
them" — the two things a price list and a cost average each get wrong alone. The shares
describe a backend's cache, so an unreliable cache shows up as a higher price.

## What it will and won't do

* **Off by default.** `routing.smart_routing: false` runs the configured priority
  order exactly as before. The console's switch flips it, persisted to the config.
* **Only reorders.** The set of backends a request may reach is the one the
  resolver produced and the auth gate left; smart routing never adds one, and
  failover still walks the rest. It cannot send a request somewhere it could not
  already go.
* **Never moves what it cannot price.** An unrankable backend, one set to `fixed`, or
  a subscription seat (no per-request price) keeps its configured position; only the
  rankable ones are permuted among the positions the rankable ones held. So an unknown
  price is never read as cheap, and priority tiers and their round-robin ties survive:
  each position keeps the priority value it had.
* **`first` / `last` always win.** `routing.backend_modes` is the "use X because I
  said so" override for a backend that is giving trouble: `first` goes ahead of
  every other backend of the model, `last` behind. They apply with smart routing
  off too. They are priority, not exclusivity — the rest remain failover targets.

## Where the numbers come from

`refresh()` re-reads the ledger every `REFRESH_SECS` from a background task and rebinds
`_prices` and `_shares`; `app/rates.py` keeps the rates and quotas. The request path only
reads those dicts. That is the one place routing depends on the ledger, deliberately, and
it is built so the ledger cannot hurt a request: a failed or empty read leaves the last
snapshot (or none), and no snapshot means the configured order. See the matching note in
AGENTS.md.
"""
import asyncio
import dataclasses
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app import config as conf
from app import ledger, rates

logger = logging.getLogger("llm-proxy")

# How often the ledger snapshot is re-read. A minute: workloads move over days, and each
# query is one indexed GROUP BY.
REFRESH_SECS = 60.0
# How far back the cache and output shares are read. Longer than the price window on
# purpose: how a backend's cache behaves changes slowly, and the shares need samples.
SHARES_DAYS = 14
# Assumed when a model has no history anywhere: mostly prompt, no cache. The same
# assumption for every backend, so it cannot favour one of them.
DEFAULT_SHARES = (0.0, 0.05)

# (asked model, provider) -> (USD per million tokens, priced requests). Observed.
_prices: Dict[Tuple[str, str], Tuple[float, int]] = {}
# (asked model, provider) -> (cache share, output share, requests). Observed.
_shares: Dict[Tuple[str, str], Tuple[float, float, int]] = {}
_refreshed_at: Optional[float] = None
_warned_at: Optional[float] = None

_INF = float("inf")


def mode_of(provider: str) -> str:
    """The effective mode of a backend. A subscription seat is always `fixed`:
    it has no per-request price to rank, and its use is not a cost decision."""
    p = conf.PROVIDERS_BY_NAME.get(provider)
    if p is not None and p.kind == conf.KIND_CLAUDE_CLI:
        return "fixed"
    return conf.ROUTING.backend_modes.get(provider, "auto")


def _shares_for(asked: str, provider: str) -> Tuple[float, float]:
    """(cache share, output share) for this model on this backend: its own history when
    it has enough, else the model's across every backend, else the default."""
    need = conf.ROUTING.smart_min_requests
    own = _shares.get((asked, provider))
    if own is not None and own[2] >= need:
        return own[0], own[1]
    rows = [v for (m, _p), v in _shares.items() if m == asked and v[2] >= need]
    if rows:
        total = sum(v[2] for v in rows)
        return (sum(v[0] * v[2] for v in rows) / total, sum(v[1] * v[2] for v in rows) / total)
    return DEFAULT_SHARES


def price_for(asked: str, target, when: Optional[datetime] = None) -> Optional[Tuple[float, str]]:
    """`(USD per million tokens, source)` for a request for `asked` on `target`, or None
    when this backend cannot be priced. `source` is `quota`, `config`, `list`,
    `observed` or `no quota`; a price of infinity is a backend that will refuse."""
    state = rates.quota_state(target.provider, target.model)
    if state == "free":
        return 0.0, "quota"
    if state == "refused":
        return _INF, "no quota"
    card = rates.card_for(target.provider, target.model)
    if card is not None:
        h, o = _shares_for(asked, target.provider)
        return rates.expected_per_mtok(card, when or datetime.now(timezone.utc), h, o), card.source
    got = _prices.get((asked, target.provider))
    if got is not None and got[1] >= conf.ROUTING.smart_min_requests:
        return got[0], "observed"
    return None


def _rank(base: list, asked: str) -> Tuple[list, dict]:
    """`base` with its rankable targets permuted into cost order, and the prices used.

    Rankable = mode `auto` and a price from `price_for`. They are sorted cheapest first
    (ties keep their configured order) and dealt back into the positions the rankable
    targets occupied, each position keeping the priority value it had — so a tie tier
    stays a tie tier and an unrankable target stays exactly where the config put it.
    """
    when = datetime.now(timezone.utc)
    positions, rankable, seen = [], [], {}
    for i, t in enumerate(base):
        if mode_of(t.provider) != "auto":
            continue
        got = price_for(asked, t, when)
        if got is None:
            continue
        seen[t.provider] = got
        positions.append(i)
        rankable.append((got[0], i, t))
    if len(rankable) < 2:
        return base, seen
    rankable.sort(key=lambda r: (r[0], r[1]))
    out = list(base)
    for pos, (_, _, t) in zip(positions, rankable):
        out[pos] = dataclasses.replace(t, priority=base[pos].priority)
    return out, seen


def _override(order: list) -> Tuple[list, bool]:
    """Apply `first` / `last`. Priorities become strictly 1..n: a forced backend
    that merely tied with another at priority 1 would still be round-robined with
    it, which is not "use X because I said so"."""
    first = [t for t in order if mode_of(t.provider) == "first"]
    last = [t for t in order if mode_of(t.provider) == "last"]
    if not first and not last:
        return order, False
    rest = [t for t in order if mode_of(t.provider) not in ("first", "last")]
    return [dataclasses.replace(t, priority=i + 1) for i, t in enumerate(first + rest + last)], True


def _num(price: float) -> Optional[float]:
    return None if price == _INF else round(price, 4)


def order(targets: list, asked: str) -> Tuple[list, Optional[dict]]:
    """The targets in the order this request should try them, plus a note on what
    was done to them (None when the order is untouched).

    `targets` is the resolver's candidate list, priority-sorted; `asked` is the
    model name the client sent, the key the ledger prices by. Pure and
    synchronous: no await, no I/O, only a read of the snapshots.
    """
    base = list(targets)
    if len(base) < 2:
        return base, None
    seen: dict = {}
    ranked = base
    if conf.ROUTING.smart_routing:
        ranked, seen = _rank(base, asked)
    smart_moved = [t.provider for t in ranked] != [t.provider for t in base]
    final, pinned = _override(ranked)
    if not smart_moved and not pinned:
        return base, None
    return final, {
        "by": "pinned" if pinned else "smart",
        "to": final[0].provider,
        "prices": {p: _num(v[0]) for p, v in seen.items()},
        "sources": {p: v[1] for p, v in seen.items()},
    }


async def refresh() -> None:
    """Re-read the ledger snapshots. Never raises: on a failure the previous snapshot
    stands, so a broken ledger can only make routing fall back to rates alone or to the
    configured order, never fail a request."""
    global _prices, _shares, _refreshed_at, _warned_at
    try:
        prices = await ledger.observed_prices(conf.ROUTING.smart_window_days)
        shares = await ledger.observed_shares(SHARES_DAYS)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - a broken ledger must not break routing
        now = time.monotonic()
        if _warned_at is None or now - _warned_at >= 300:
            _warned_at = now
            logger.warning("Smart routing: could not read the ledger (%s: %s) — "
                           "keeping the last snapshot", type(e).__name__, e)
        return
    _prices, _shares = prices, shares
    _refreshed_at = time.time()


async def refresh_loop() -> None:
    """Keep the ledger snapshot fresh for the life of the process (lifespan task)."""
    while True:
        await refresh()
        await asyncio.sleep(REFRESH_SECS)


def _logical_targets(name: str, lm) -> List[conf.Target]:
    return [
        conf.Target(provider=t.provider, model=conf.native_for(name, t.provider, t.model),
                    priority=t.priority)
        for t in lm.targets
    ]


def _price_rows() -> list:
    """Every (model, backend) pair the ranking could price, with the price it would use."""
    pairs: Dict[Tuple[str, str], conf.Target] = {}
    for name, lm in conf.LOGICAL_MODELS.items():
        for t in _logical_targets(name, lm):
            pairs[(name, t.provider)] = t
    for key in list(_prices) + list(_shares):
        if key not in pairs and key[1] in conf.PROVIDERS_BY_NAME:
            pairs[key] = conf.Target(provider=key[1], model=conf.native_for(key[0], key[1]))
    now = datetime.now(timezone.utc)
    rows = []
    for (model, provider), t in sorted(pairs.items()):
        got = price_for(model, t, now)
        if got is None:
            continue
        seen = _prices.get((model, provider)) or (0.0, 0)
        shared = _shares.get((model, provider))
        rows.append({
            "model": model, "provider": provider, "usd_per_mtok": _num(got[0]), "source": got[1],
            "requests": shared[2] if shared else seen[1],
        })
    return rows


def snapshot() -> dict:
    """What the Routing tab shows: the switch and tunables, each backend's mode,
    every price the ranking would use and where it came from, and for each configured
    logical model the order it has now against the order the config gives it."""
    r = conf.ROUTING
    models = []
    for name, lm in conf.LOGICAL_MODELS.items():
        base = sorted(_logical_targets(name, lm), key=lambda t: t.priority)
        now, note = order(base, name)
        models.append({
            "name": name,
            "static": [t.provider for t in base],
            "current": [t.provider for t in now],
            "by": note["by"] if note else None,
        })
    return {
        "enabled": r.smart_routing,
        "window_days": r.smart_window_days,
        "min_requests": r.smart_min_requests,
        "refreshed_at": _refreshed_at,
        "providers": [
            {"name": p.name, "kind": p.kind, "mode": mode_of(p.name),
             "locked": p.kind == conf.KIND_CLAUDE_CLI}
            for p in conf.PROVIDERS
        ],
        "prices": _price_rows(),
        "models": models,
    }
