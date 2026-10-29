"""Warnings to ntfy, and what an empty balance does to routing.

ntfy is an `httpx.MockTransport` on the shared account client, so the tests see the exact
request a phone would be sent: title, priority, tags, bearer token and body. What matters
is the behaviour no review can see: that a restart does not re-announce what is already
true, that one condition is one message, that a failed send is retried rather than lost,
and that nothing here can fail or slow a request.
"""
import asyncio
import logging

import httpx
import pytest

from app import config as conf
from app import notify, rates, upstream
from app.providers import Balance, Quota

CONFIG = """\
providers:
  - {name: direct, base_url: "https://api.deepseek.com", api_key: "sk-ds"}
  - {name: nano,   base_url: "https://nano-gpt.com/api", api_key: "sk-nano"}
  - {name: router, base_url: "https://openrouter.ai/api", api_key: "sk-or"}
"""
URL = "http://ntfy:80/llm-proxy"


@pytest.fixture
def ntfy(load_config, monkeypatch):
    """Config with three vendor backends, ntfy configured, and a mock ntfy that records
    every message. `ntfy.fail` makes it answer 500 (or raise, if set to an exception)."""
    load_config(CONFIG)
    monkeypatch.setattr(conf, "NOTIFY", conf.Notify(url=URL, token="tk_secret", low_balance=1.0,
                                                    low_balance_by={"router": 5.0}, reminder_hours=12.0))
    box = type("Box", (), {})()
    box.sent, box.fail = [], None

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(box.fail, Exception):
            raise box.fail
        if box.fail:
            return httpx.Response(box.fail)
        box.sent.append({"url": str(request.url), "headers": dict(request.headers), "body": request.content.decode()})
        return httpx.Response(200, json={"id": "x"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(upstream, "account_client", lambda: client)
    box.titles = lambda: [m["headers"]["title"] for m in box.sent]
    return box


def balance(provider, amount, currency="USD"):
    rates._balances[provider] = Balance(currency, amount)
    rates._account_at[provider] = 1.0


def quota(**kw):
    base = dict(used=10, limit=100, remaining=90, reset_at=1792972800.0, allow_overage=True, available=True)
    base.update(kw)
    return Quota(**base)


H = 3600.0


# ── configuration ───────────────────────────────────────────────────────────

def test_notifications_are_off_unless_ntfy_is_configured(load_config):
    load_config(CONFIG)
    assert conf.NOTIFY.enabled is False and conf.NOTIFY.low_balance == 1.0 and conf.NOTIFY.reminder_hours == 12.0


def test_the_notify_section_parses_and_reads_the_token_from_the_environment(load_config, monkeypatch):
    monkeypatch.setenv("NTFY_TOKEN", "tk_from_env")
    load_config(CONFIG + """
notify:
  ntfy: http://ntfy:80/llm-proxy
  token: ${NTFY_TOKEN}
  low_balance: 2.5
  low_balance_by: {direct: 4, router: 10}
  reminder_hours: 6
""")
    n = conf.NOTIFY
    assert (n.url, n.token, n.low_balance, n.reminder_hours) == (URL, "tk_from_env", 2.5, 6.0)
    assert n.threshold("direct") == 4.0 and n.threshold("nano") == 2.5


def test_a_non_numeric_threshold_fails_the_load(load_config):
    with pytest.raises(ValueError, match="notify"):
        load_config(CONFIG + "notify: {ntfy: 'http://x', low_balance: plenty}\n")


def test_a_402_is_a_failover_status_by_default():
    assert 402 in conf.Routing().failover_statuses


# ── what is announced, and when ─────────────────────────────────────────────

async def test_nothing_is_sent_when_ntfy_is_not_configured(ntfy, monkeypatch):
    monkeypatch.setattr(conf, "NOTIFY", conf.Notify())
    balance("direct", 0.0)
    assert await notify.run_once(0.0) == 0 and ntfy.sent == []
    assert await notify.send(notify.Notice("t", "m")) is False


async def test_a_condition_already_true_at_start_is_known_not_news(ntfy):
    """A deploy restarts the process; it must not re-announce a balance you already know is low."""
    balance("direct", 0.40)
    assert await notify.run_once(0.0) == 0
    assert await notify.run_once(60.0) == 0 and ntfy.sent == []


async def test_a_balance_that_drops_after_start_is_announced_once(ntfy):
    balance("direct", 3.79)
    await notify.run_once(0.0)                                   # seeds: all fine
    balance("direct", 0.50)
    assert await notify.run_once(60.0) == 1
    sent = ntfy.sent[0]
    assert httpx.URL(sent["url"]) == httpx.URL(URL) and sent["headers"]["authorization"] == "Bearer tk_secret"
    assert sent["headers"]["title"] == "direct: balance is low" and sent["headers"]["priority"] == "default"
    assert sent["headers"]["tags"] == "warning,moneybag"
    assert "$0.50" in sent["body"] and "$1.00" in sent["body"]
    assert await notify.run_once(120.0) == 0 and await notify.run_once(600.0) == 0, "one condition, one message"


async def test_each_backend_has_its_own_threshold(ntfy):
    balance("router", 9.0); balance("direct", 9.0)
    await notify.run_once(0.0)
    balance("router", 4.0); balance("direct", 4.0)               # router warns under 5, direct under 1
    await notify.run_once(60.0)
    assert ntfy.titles() == ["router: balance is low"]


async def test_running_out_is_louder_than_running_low_and_does_not_also_say_topped_up(ntfy):
    balance("direct", 3.0)
    await notify.run_once(0.0)
    balance("direct", 0.60); await notify.run_once(60.0)
    balance("direct", 0.0); await notify.run_once(120.0)
    assert ntfy.titles() == ["direct: balance is low", "direct: balance is empty"]
    assert ntfy.sent[1]["headers"]["priority"] == "high" and "rotating_light" in ntfy.sent[1]["headers"]["tags"]


async def test_a_warning_that_stays_true_is_repeated_after_the_reminder_period(ntfy):
    balance("direct", 3.0)
    await notify.run_once(0.0)
    balance("direct", 0.5); await notify.run_once(60.0)
    assert await notify.run_once(60.0 + 11 * H) == 0
    assert await notify.run_once(60.0 + 13 * H) == 1 and len(ntfy.sent) == 2


async def test_topping_up_clears_the_warning_and_says_so(ntfy):
    balance("direct", 3.0)
    await notify.run_once(0.0)
    balance("direct", 0.5); await notify.run_once(60.0)
    balance("direct", 20.0)
    assert await notify.run_once(120.0) == 1
    assert ntfy.titles()[-1] == "direct: balance topped up" and ntfy.sent[-1]["headers"]["priority"] == "low"
    assert await notify.run_once(180.0) == 0
    balance("direct", 0.4)                                       # and it can warn again afterwards
    assert await notify.run_once(240.0) == 1


async def test_a_failed_send_is_retried_not_lost(ntfy):
    balance("direct", 3.0)
    await notify.run_once(0.0)
    balance("direct", 0.5)
    ntfy.fail = 500
    assert await notify.run_once(60.0) == 0 and ntfy.sent == []
    ntfy.fail = None
    assert await notify.run_once(120.0) == 1, "the next pass delivers it; it did not wait out the reminder"


async def test_an_unreachable_ntfy_never_raises_and_warns_once(ntfy, caplog):
    ntfy.fail = httpx.ConnectError("refused")
    caplog.set_level(logging.WARNING, logger="llm-proxy")
    for _ in range(3):
        assert await notify.send(notify.Notice("t", "m")) is False
    assert len([r for r in caplog.records if "Notify: could not reach ntfy" in r.getMessage()]) == 1


async def test_a_subscription_used_up_is_announced_with_what_happens_next(ntfy):
    balance("nano", 15.99)
    rates._quotas["nano"] = quota()
    await notify.run_once(0.0)
    rates._quotas["nano"] = quota(used=60000100, limit=60000000, remaining=0, available=False)
    assert await notify.run_once(60.0) == 1
    body = ntfy.sent[0]["body"]
    assert ntfy.titles() == ["nano: subscription allowance used up"]
    assert "bill the balance" in body and "$15.99" in body and "Mon 26 Oct 00:00 BRT" not in body and "BRT" in body


async def test_a_counter_parked_under_the_limit_is_announced_as_used_up(ntfy):
    """The counter can stop short of the limit for good (2026-10-28: 821 left); what makes
    the allowance spent is `available`, not a remaining of zero."""
    balance("nano", 15.75)
    rates._quotas["nano"] = quota()
    await notify.run_once(0.0)
    rates._quotas["nano"] = quota(used=59999179, limit=60000000, remaining=821, available=False)
    assert await notify.run_once(60.0) == 1
    assert ntfy.titles() == ["nano: subscription allowance used up"]


async def test_without_overage_a_spent_subscription_says_requests_are_refused(ntfy):
    balance("nano", 15.99)
    rates._quotas["nano"] = quota()
    await notify.run_once(0.0)
    rates._quotas["nano"] = quota(remaining=0, available=False, allow_overage=False)
    await notify.run_once(60.0)
    assert "refused until it resets" in ntfy.sent[0]["body"]


# ── a payment refused on a live request ─────────────────────────────────────

async def test_a_402_on_a_live_request_is_announced_at_most_once_an_hour_per_backend(ntfy):
    balance("direct", 0.0)
    notify.payment_required("direct", "deepseek-flash")
    notify.payment_required("direct", "deepseek-flash")
    notify.payment_required("router", "m")
    await asyncio.gather(*list(notify._tasks))
    assert sorted(ntfy.titles()) == ["direct: payment required (402)", "router: payment required (402)"]
    assert "for deepseek-flash" in ntfy.sent[0]["body"] and "$0.00" in ntfy.sent[0]["body"]


async def test_payment_required_is_a_no_op_when_ntfy_is_not_configured(ntfy, monkeypatch):
    monkeypatch.setattr(conf, "NOTIFY", conf.Notify())
    notify.payment_required("direct", "m")
    assert not notify._tasks and ntfy.sent == []


# ── an empty balance and routing ────────────────────────────────────────────

def test_a_backend_with_no_money_is_out_of_funds(ntfy):
    assert rates.out_of_funds("direct") is False, "an unread balance is assumed fine"
    balance("direct", 3.79)
    assert rates.out_of_funds("direct") is False and rates.funds_state("direct") == "ok"
    balance("direct", 0.80)
    assert rates.out_of_funds("direct") is False and rates.funds_state("direct") == "low"
    balance("direct", 0.01)
    assert rates.out_of_funds("direct") is True and rates.funds_state("direct") == "empty"


def test_a_subscription_with_allowance_left_pays_for_itself_whatever_the_balance(ntfy):
    balance("nano", 0.0)
    assert rates.out_of_funds("nano") is True
    rates._quotas["nano"] = quota()
    assert rates.out_of_funds("nano") is False
    rates._quotas["nano"] = quota(remaining=0, available=False)
    assert rates.out_of_funds("nano") is True
