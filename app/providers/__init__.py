"""What each vendor lets us read about price, quota and balance.

One module per vendor — `openrouter`, `nanogpt`, `deepseek` — each with up to four
functions, all taking the shared `httpx.AsyncClient` and the `Provider` they serve:

    fetch_rates(client, provider)    -> {native id: RateCard}      published prices
    fetch_included(client, provider) -> {native id, ...}           models a subscription covers
    fetch_quota(client, provider)    -> Quota | None               subscription allowance left
    fetch_balance(client, provider)  -> Balance | None             money left

A vendor module may also define `adapt_request(payload) -> payload`, applied by
`proxy._build_body` to the request on its way to a backend of that vendor, for the
differences between OpenAI-compatible APIs that break a conversation moved between them
(see `deepseek`).

A vendor omits a function it has no API for (DeepSeek publishes no rates), and the
caller asks with `getattr`. The fetchers only talk and parse; what to do with the
numbers lives in `app/rates.py`. The parsers are pure functions of a decoded JSON
payload, so each is tested against a response shaped like the real one.

The shapes and units of every endpoint are written down in `docs/providers/`.
Units differ per vendor — USD per token, per thousand, per million — and each parser
converts to the one unit used everywhere else here: **USD per million tokens**.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from app.config import Rate

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Price keys an override may set, mapped to the field of `Rate` they replace.
_KEYS = {"prompt": "input", "input_cache_read": "cached", "completion": "output"}


@dataclass(frozen=True)
class Override:
    """A conditional price: inside its UTC window it replaces some of the rates.

    `days` are weekday indexes (Monday 0), or None for every day. `start`/`end` are
    minutes since UTC midnight, `end` exclusive; an `end` at or before `start` wraps
    past midnight (10:00 to 00:00 is a window that runs to the end of the day), and
    no window at all means the whole day. `prices` holds only the rates the entry
    names, in USD per million tokens — whatever it omits keeps the base price."""
    prices: Tuple[Tuple[str, float], ...]
    days: Optional[frozenset] = None
    start: Optional[int] = None
    end: Optional[int] = None

    def applies(self, when: datetime) -> bool:
        when = when.astimezone(timezone.utc)
        if self.days is not None and when.weekday() not in self.days:
            return False
        if self.start is None and self.end is None:
            return True
        now = when.hour * 60 + when.minute
        start, end = self.start or 0, self.end or 0
        if start == end:
            return True
        return start <= now < end if start < end else (now >= start or now < end)


@dataclass(frozen=True)
class RateCard:
    """One model's published price: a base `Rate` and the overrides that change it
    at certain times. `source` says where it came from (`list` for a vendor's
    catalog, `config` for the operator's own table)."""
    base: Rate
    overrides: Tuple[Override, ...] = ()
    source: str = "list"

    def at(self, when: datetime) -> Rate:
        """The rates in force at `when`: the base, then each applicable override in
        order, later ones winning per key."""
        values = {"input": self.base.input, "cached": self.base.cached, "output": self.base.output}
        for o in self.overrides:
            if o.applies(when):
                values.update(dict(o.prices))
        return Rate(**values)


@dataclass(frozen=True)
class Quota:
    """A subscription allowance, in the vendor's own unit (input tokens, for nano).

    `available` is the vendor's verdict where it gives one — NanoGPT's
    `recommendedMode` — and "something is left" where it does not. A quota that is
    used up and does not allow overage is a backend that will refuse the request."""
    used: int
    limit: int
    remaining: int
    reset_at: Optional[float]          # epoch seconds
    allow_overage: bool
    available: bool
    mode: Optional[str] = None         # the vendor's recommended billing mode, if it says

    @property
    def percent(self) -> float:
        return self.used / self.limit if self.limit else 0.0


@dataclass(frozen=True)
class Balance:
    currency: str
    amount: float


def to_float(value) -> Optional[float]:
    """A number the vendors send as a string, a float or an int; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def day_index(name) -> Optional[int]:
    try:
        return _DAYS.index(str(name).lower())
    except ValueError:
        return None


def hhmm_to_minutes(value) -> Optional[int]:
    """`1030` -> 630. The vendors write a clock time as an HHMM number."""
    n = to_float(value)
    if n is None or n < 0:
        return None
    n = int(n)
    return (n // 100) * 60 + n % 100


def get_vendor(name: str):
    """The module for a vendor name, imported on demand."""
    import importlib
    return importlib.import_module(f"app.providers.{name}")
