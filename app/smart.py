"""Smart routing: order a model's backends by what they have actually cost.

A logical model can run on several backends, and `priority` says which to try
first. That order is a decision made once, in the config — usually "the cheap
quota first". Smart routing is the other way to decide it: **per request, cheapest
backend first**, using the prices the cost ledger has observed for that model on
each backend over the last `routing.smart_window_days`.

Observed, not list, price on purpose. A list price cannot see that one backend's
prompt cache is unreliable, that another doubles in a peak window, or how many
reasoning tokens a model burns — the ledger's dollars-per-million-tokens already
contains all of it. The cost of that choice is a feedback loop (the backend that is
chosen warms its cache and gets cheaper, which keeps it chosen) and no sense of the
hour of day; both are worth knowing, neither worth hiding.

## What it will and won't do

* **Off by default.** `routing.smart_routing: false` runs the configured priority
  order exactly as before. The console's switch flips it, persisted to the config.
* **Only reorders.** The set of backends a request may reach is the one the
  resolver produced and the auth gate left; smart routing never adds one, and
  failover still walks the rest. It cannot send a request somewhere it could not
  already go.
* **Never moves what it cannot price.** A backend with fewer than
  `smart_min_requests` priced requests for this model (or `fixed`, or a
  subscription seat, which has no per-request price) keeps its configured
  position; only the rankable ones are permuted among the positions the rankable
  ones held. So an unknown price is never read as cheap, and priority tiers and
  their round-robin ties survive: each position keeps the priority value it had.
* **`first` / `last` always win.** `routing.backend_modes` is the "use X because I
  said so" override for a backend that is giving trouble: `first` goes ahead of
  every other backend of the model, `last` behind. They apply with smart routing
  off too. They are priority, not exclusivity — the rest remain failover targets.

## Where the prices come from

`refresh()` re-reads the ledger every `REFRESH_SECS` from a background task and
rebinds `_prices`; the request path only reads that dict. That is the one place
routing depends on the ledger, deliberately, and it is built so the ledger cannot
hurt a request: a failed or empty read leaves the last snapshot (or none), and no
snapshot means the configured order. See the matching note in AGENTS.md.
"""
import asyncio
import dataclasses
import logging
import time
from typing import Dict, List, Optional, Tuple

from app import config as conf
from app import ledger

logger = logging.getLogger("llm-proxy")

# How often the price snapshot is re-read. A minute: prices move over days, and the
# query is one indexed GROUP BY.
REFRESH_SECS = 60.0

# (asked model, provider) -> (USD per million tokens, priced requests). Rebound
# whole by `refresh`; never mutated in place, so a request sees one snapshot.
_prices: Dict[Tuple[str, str], Tuple[float, int]] = {}
_refreshed_at: Optional[float] = None
_warned_at: Optional[float] = None


def mode_of(provider: str) -> str:
    """The effective mode of a backend. A subscription seat is always `fixed`:
    it has no per-request price to rank, and its use is not a cost decision."""
    p = conf.PROVIDERS_BY_NAME.get(provider)
    if p is not None and p.kind == conf.KIND_CLAUDE_CLI:
        return "fixed"
    return conf.ROUTING.backend_modes.get(provider, "auto")


def _rank(base: list, asked: str) -> list:
    """`base` with its rankable targets permuted into cost order.

    Rankable = mode `auto` and a price from at least `smart_min_requests`. They are
    sorted cheapest first (ties keep their configured order) and dealt back into
    the positions the rankable targets occupied, each position keeping the priority
    value it had — so a tie tier stays a tie tier and an unrankable target stays
    exactly where the config put it.
    """
    min_requests = conf.ROUTING.smart_min_requests
    positions, rankable = [], []
    for i, t in enumerate(base):
        if mode_of(t.provider) != "auto":
            continue
        got = _prices.get((asked, t.provider))
        if got is None or got[1] < min_requests:
            continue
        positions.append(i)
        rankable.append((got[0], i, t))
    if len(rankable) < 2:
        return base
    rankable.sort(key=lambda r: (r[0], r[1]))
    out = list(base)
    for pos, (_, _, t) in zip(positions, rankable):
        out[pos] = dataclasses.replace(t, priority=base[pos].priority)
    return out


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


def order(targets: list, asked: str) -> Tuple[list, Optional[dict]]:
    """The targets in the order this request should try them, plus a note on what
    was done to them (None when the order is untouched).

    `targets` is the resolver's candidate list, priority-sorted; `asked` is the
    model name the client sent, the key the ledger prices by. Pure and
    synchronous: no await, no I/O, only a read of the price snapshot.
    """
    base = list(targets)
    if len(base) < 2:
        return base, None
    ranked = _rank(base, asked) if conf.ROUTING.smart_routing else base
    smart_moved = [t.provider for t in ranked] != [t.provider for t in base]
    final, pinned = _override(ranked)
    if not smart_moved and not pinned:
        return base, None
    by = "pinned" if pinned else "smart"
    prices = {
        t.provider: round(_prices[(asked, t.provider)][0], 4)
        for t in final if (asked, t.provider) in _prices
    }
    return final, {"by": by, "to": final[0].provider, "prices": prices}


async def refresh() -> None:
    """Re-read the price snapshot from the ledger. Never raises: on a failure the
    previous snapshot stands, so a broken ledger can only make routing fall back
    to the configured order, never fail a request."""
    global _prices, _refreshed_at, _warned_at
    try:
        fresh = await ledger.observed_prices(conf.ROUTING.smart_window_days)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - a broken ledger must not break routing
        now = time.monotonic()
        if _warned_at is None or now - _warned_at >= 300:
            _warned_at = now
            logger.warning("Smart routing: could not read observed prices (%s: %s) — "
                           "keeping the last snapshot", type(e).__name__, e)
        return
    _prices = fresh
    _refreshed_at = time.time()


async def refresh_loop() -> None:
    """Keep the snapshot fresh for the life of the process (lifespan task)."""
    while True:
        await refresh()
        await asyncio.sleep(REFRESH_SECS)


def _logical_targets(name: str, lm) -> List[conf.Target]:
    return [
        conf.Target(provider=t.provider, model=conf.native_for(name, t.provider, t.model),
                    priority=t.priority)
        for t in lm.targets
    ]


def snapshot() -> dict:
    """What the Routing tab shows: the switch and tunables, each backend's mode,
    every price the ranking would use, and for each configured logical model the
    order it has now against the order the config gives it."""
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
        "prices": [
            {"model": m, "provider": p, "usd_per_mtok": price, "requests": n}
            for (m, p), (price, n) in sorted(_prices.items())
        ],
        "models": models,
    }
