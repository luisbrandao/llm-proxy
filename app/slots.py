"""Per-provider concurrency control with priority-ordered admission.

Each provider has a slot budget (its `slots` config; None = unlimited). A request
carries an ordered list of candidate targets (highest priority first). Admission
walks the priority tiers best-first and takes a slot from a free provider in the
first tier that has one; within a tier it round-robins across the free providers
so equal-priority backends share load evenly. If nothing is free the request waits
on a shared Condition until some other request releases a slot, then re-scans — so
priority is honored on every wake and waiters queue fairly (Condition wakes them
roughly FIFO).

State is process-local, which is correct here because the app runs as a single
uvicorn worker. Running multiple workers would split the accounting and must use
a shared store instead.
"""
import asyncio
from itertools import groupby

from app import config as conf
from app.metrics import QUEUE_WAITING, SLOTS_IN_USE

# Created lazily on first use so it binds to the running event loop (uvicorn's),
# not whatever loop happened to exist at import time.
_cond = None
_in_use = {}  # provider name -> current in-flight count
_rr = {}      # round-robin cursor per set of equal-priority free providers


def _condition() -> asyncio.Condition:
    global _cond
    if _cond is None:
        _cond = asyncio.Condition()
    return _cond


class SlotTimeout(Exception):
    """Raised when no slot becomes free within the configured queue timeout."""


def in_use(provider_name: str) -> int:
    """Current in-flight count for a provider (0 if idle). Read-only view of the
    live slot accounting, for introspection (e.g. the admin routing view)."""
    return _in_use.get(provider_name, 0)


def _capacity(provider_name: str):
    p = conf.PROVIDERS_BY_NAME.get(provider_name)
    return p.slots if p else None  # None => unlimited


def _free(provider_name: str) -> bool:
    cap = _capacity(provider_name)
    if cap is None:
        return True
    return _in_use.get(provider_name, 0) < cap


def _take(provider_name: str) -> None:
    _in_use[provider_name] = _in_use.get(provider_name, 0) + 1
    SLOTS_IN_USE.labels(provider=provider_name).set(_in_use[provider_name])


def _pick_free(targets):
    """Best free target: first priority tier with a free provider, round-robined.

    `targets` is already priority-sorted. Within a tier we rotate across the
    providers that currently have a free slot so equal-priority backends share
    load; a single free provider is returned directly. None => nothing free.
    """
    for prio, tier in groupby(targets, key=lambda t: t.priority):
        free = [t for t in tier if _free(t.provider)]
        if not free:
            continue
        if len(free) == 1:
            return free[0]
        key = (prio,) + tuple(t.provider for t in free)
        i = _rr.get(key, 0) % len(free)
        _rr[key] = i + 1
        return free[i]
    return None


async def acquire(targets, timeout: float = 0.0):
    """Reserve a slot on the best available target. Waits if all are full.

    `targets` is ordered by priority. `timeout` of 0 (or None) waits forever;
    otherwise SlotTimeout is raised once the deadline passes.
    """
    cond = _condition()
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout if timeout and timeout > 0 else None

    async with cond:
        waiting = False
        try:
            while True:
                t = _pick_free(targets)
                if t is not None:
                    _take(t.provider)
                    return t
                # Nothing free: queue until a slot is released.
                if not waiting:
                    waiting = True
                    QUEUE_WAITING.inc()
                if deadline is None:
                    await cond.wait()
                else:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise SlotTimeout()
                    try:
                        await asyncio.wait_for(cond.wait(), remaining)
                    except asyncio.TimeoutError:
                        raise SlotTimeout()
        finally:
            if waiting:
                QUEUE_WAITING.dec()


async def release(provider_name: str) -> None:
    cond = _condition()
    async with cond:
        current = _in_use.get(provider_name, 0)
        if current > 0:
            _in_use[provider_name] = current - 1
            SLOTS_IN_USE.labels(provider=provider_name).set(current - 1)
        cond.notify()
