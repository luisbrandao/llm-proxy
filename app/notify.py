"""Warnings to ntfy: a balance running low or empty, an allowance used up, a payment refused.

Nobody watches the console all day, and the cheapest way to find out a prepaid account is
empty is for requests to start failing. This reads the same snapshot the Costs tab shows
(`rates`: balances and subscription quota, refreshed in the background) and pushes a
message when something needs a human.

## What it says, and when

* **balance low** — under the account's threshold (`notify.low_balance`, or its entry in
  `low_balance_by`), the earliest warning;
* **balance empty** — no money left: requests to it are skipped and fail over;
* **allowance used up** — a subscription (NanoGPT) is spent, so requests now bill the
  balance, or are refused if overage is off;
* **payment refused** — a backend answered 402 on a live request, which catches an
  account emptying between two reads;
* **topped up** — a balance warning cleared.

One message when a condition *becomes* true, a reminder every `reminder_hours` while it
stays true, a recovery message when it clears. Not one per check.

## What it will not do

* **Block or fail a request.** The only call from the request path, `payment_required`,
  decides synchronously and sends from a background task; every send swallows its errors.
* **Repeat itself because of a deploy.** State is in memory, so a restart forgets what was
  sent. The first evaluation after a start that finds data records what is already true
  *without* sending it: you know about it, and a deploy is not news. Only a condition that
  becomes true afterwards is announced.
* **Lose an alert to a bad moment.** A message counts as sent only when ntfy accepted it, so
  an outage on the way is retried on the next pass instead of waiting out the reminder.
* **Do anything unless `notify.ntfy` is set.**

The ntfy topic is a URL (`http://ntfy:80/llm-proxy` from inside the stack); `notify.token`
is sent as a Bearer token when the server requires one.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from app import config as conf
from app import rates, upstream

logger = logging.getLogger("llm-proxy")

CHECK_EVERY = 60.0
# The first account read lands a few seconds after start; evaluating before it would
# find nothing and then treat the first real reading as news.
FIRST_CHECK_AFTER = 45.0
# A 402 on a live request is announced at most this often per backend.
PAYMENT_REQUIRED_EVERY = 3600.0
WARN_EVERY = 900.0

_sent: Dict[Tuple[str, str], float] = {}      # (provider, kind) -> monotonic time last announced
_seeded = False
_payment_at: Dict[str, float] = {}
_tasks: set = set()
_warned_at: Optional[float] = None


@dataclass(frozen=True)
class Notice:
    title: str
    message: str
    priority: str = "default"     # min | low | default | high | urgent
    tags: Tuple[str, ...] = ()     # ntfy turns known tag names into emoji


def _money(amount: float, currency: str) -> str:
    return f"${amount:,.2f}" if currency == "USD" else f"{amount:,.2f} {currency}"


def _when(epoch: Optional[float]) -> str:
    if not epoch:
        return "later"
    brt = timezone(timedelta(hours=-3))
    return datetime.fromtimestamp(epoch, brt).strftime("%a %d %b %H:%M BRT")


def _conditions() -> Dict[Tuple[str, str], Notice]:
    """Every warning that is true right now, from the account snapshot."""
    out: Dict[Tuple[str, str], Notice] = {}
    for a in rates.accounts_snapshot()["accounts"]:
        name, b, q = a["name"], a["balance"], a["quota"]
        if b is not None:
            amount = _money(b["amount"], b["currency"])
            if a["state"] == "empty":
                out[(name, "balance_empty")] = Notice(
                    f"{name}: balance is empty",
                    f"{name} has {amount} left. Requests to it are skipped and fail over to the "
                    f"next backend; top it up to use it again.",
                    "high", ("rotating_light", "moneybag"))
            elif a["state"] == "low":
                out[(name, "balance_low")] = Notice(
                    f"{name}: balance is low",
                    f"{name} has {amount} left, under the {_money(conf.NOTIFY.threshold(name), b['currency'])} warning level.",
                    "default", ("warning", "moneybag"))
        if q is not None and (not q["available"]) and q["remaining"] <= 0:
            billing = (f"requests now bill the balance"
                       + (f" ({_money(b['amount'], b['currency'])} left)" if b else "")
                       + " at list prices") if q["allow_overage"] else "requests are refused until it resets"
            out[(name, "quota_used")] = Notice(
                f"{name}: subscription allowance used up",
                f"{q['used'] / 1e6:.1f}M of {q['limit'] / 1e6:.0f}M tokens used; {billing}. "
                f"It resets {_when(q['reset_at'])}.",
                "default", ("hourglass",))
    return out


def evaluate(now: Optional[float] = None) -> List[Tuple[Optional[Tuple[str, str]], Notice]]:
    """What to announce now, as `(key, notice)`; `key` is None for a recovery message.

    Pure apart from seeding: it never sends and never marks anything sent — `run_once`
    does that after ntfy accepts the message."""
    global _seeded
    now = time.monotonic() if now is None else now
    snapshot = rates.accounts_snapshot()["accounts"]
    current = _conditions()
    seeding = not _seeded and bool(snapshot)
    reminder = conf.NOTIFY.reminder_hours * 3600
    out: List[Tuple[Optional[Tuple[str, str]], Notice]] = []
    for key, notice in current.items():
        last = _sent.get(key)
        if last is None:
            if seeding:
                _sent[key] = now          # already true when we started: known, not news
            else:
                out.append((key, notice))
        elif now - last >= reminder:
            out.append((key, notice))
    for key in [k for k in _sent if k not in current]:
        del _sent[key]
        provider, kind = key
        if kind.startswith("balance_") and not any(k[0] == provider and k[1].startswith("balance_") for k in current):
            if not seeding:
                b = next((a["balance"] for a in snapshot if a["name"] == provider), None)
                left = f" ({_money(b['amount'], b['currency'])} now)" if b else ""
                out.append((None, Notice(f"{provider}: balance topped up",
                                         f"{provider} is back above its warning level{left}.",
                                         "low", ("white_check_mark",))))
    if seeding:
        _seeded = True
    return out


async def send(notice: Notice) -> bool:
    """Publish one message. True only when ntfy accepted it. Never raises."""
    global _warned_at
    cfg = conf.NOTIFY
    if not cfg.enabled:
        return False
    headers = {"Title": notice.title, "Priority": notice.priority, "User-Agent": "llm-proxy"}
    if notice.tags:
        headers["Tags"] = ",".join(notice.tags)
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    try:
        resp = await upstream.account_client().post(cfg.url, content=notice.message.encode("utf-8"), headers=headers)
        resp.raise_for_status()
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - a warning that cannot be delivered must not break anything
        now = time.monotonic()
        if _warned_at is None or now - _warned_at >= WARN_EVERY:
            _warned_at = now
            logger.warning("Notify: could not reach ntfy at %s (%s: %s)", cfg.url, type(e).__name__, e)
        return False


async def run_once(now: Optional[float] = None) -> int:
    """Evaluate, send, and mark sent what ntfy accepted. Returns how many were delivered."""
    now = time.monotonic() if now is None else now
    delivered = 0
    for key, notice in evaluate(now):
        if await send(notice):
            delivered += 1
            if key is not None:
                _sent[key] = now
    return delivered


async def loop() -> None:
    """The lifespan task. Idle until `notify:` is configured, picks that up on a reload."""
    await asyncio.sleep(FIRST_CHECK_AFTER)
    while True:
        try:
            if conf.NOTIFY.enabled:
                await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never let the checker die
            logger.warning("Notify: check failed (%s: %s)", type(e).__name__, e)
        await asyncio.sleep(CHECK_EVERY)


def payment_required(provider: str, model: Optional[str] = None) -> None:
    """A backend answered 402 on a live request. Called from the request path, so it only
    decides (cheaply, synchronously) and hands the send to a background task."""
    if not conf.NOTIFY.enabled:
        return
    now = time.monotonic()
    last = _payment_at.get(provider)
    if last is not None and now - last < PAYMENT_REQUIRED_EVERY:
        return
    _payment_at[provider] = now
    b = rates._balances.get(provider)
    seen = f" Balance as last read: {_money(b.amount, b.currency)}." if b else ""
    notice = Notice(
        f"{provider}: payment required (402)",
        f"{provider} refused a request{' for ' + model if model else ''} with HTTP 402, so its balance "
        f"is probably empty. The request was moved to the next backend.{seen}",
        "high", ("rotating_light", "moneybag"))
    try:
        task = asyncio.get_running_loop().create_task(send(notice))
    except RuntimeError:        # no running loop: nothing to schedule on
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def reset() -> None:
    """Forget everything (tests)."""
    global _seeded, _warned_at
    _sent.clear(); _payment_at.clear(); _tasks.clear()
    _seeded, _warned_at = False, None
