"""The rates service: what it keeps fresh, how a subscription counts, and that a vendor
failing can age the data but never empty it or reach a request.

Upstream is an `httpx.MockTransport` on the shared account client, answering with
payloads shaped like the live ones (fixtures in test_providers.py).
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx
import pytest

from app import config as conf
from app import rates, upstream
from app.config import Peak, Pricing, Rate
from app.providers import Quota, RateCard
from tests.test_providers import NANO_MODELS, NANO_USAGE, OPENROUTER_MODELS

CONFIG = """\
models:
  glm-5.3:
    targets:
      - {provider: nano, priority: 1}
      - {provider: router, priority: 2}

providers:
  - name: nano
    base_url: "https://nano-gpt.com/api"
    api_key: "sk-nano-secret"
    enabled_models: ["z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash-1017:thinking", "moonshotai/kimi-k3"]
    model_map:
      "z-ai/glm-5.3": glm-5.3
  - name: router
    base_url: "https://openrouter.ai/api"
    api_key: "sk-or-secret"
    model_map:
      "z-ai/glm-5.3": glm-5.3
  - name: direct
    base_url: "https://api.deepseek.com"
    api_key: "sk-ds-secret"
    enabled_models: [deepseek-flash]
  - name: local
    base_url: "http://box.lan:11434"
"""

CREDITS = {"data": {"total_credits": 270, "total_usage": 252.275343764}}
DS_BALANCE = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "3.87"}]}


@pytest.fixture
def vendors(load_config, monkeypatch):
    """Install the config and a mock for every vendor endpoint; returns the call log
    and a dict of handlers a test can override."""
    load_config(CONFIG)
    calls, override = [], {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), dict(request.headers)))
        url = str(request.url)
        for needle, make in override.items():
            if needle in url:
                return make(request)
        if "openrouter.ai/api/v1/models" in url:
            return httpx.Response(200, json=OPENROUTER_MODELS)
        if "openrouter.ai/api/v1/credits" in url:
            return httpx.Response(200, json=CREDITS)
        if "nano-gpt.com/api/v1/models" in url:
            return httpx.Response(200, json=NANO_MODELS)
        if "subscription/v1/models" in url:
            return httpx.Response(200, json={"data": [{"id": "z-ai/glm-5.3"},
                                                      {"id": "deepseek/deepseek-v4.1-flash-1017:thinking"}]})
        if "subscription/v1/usage" in url:
            return httpx.Response(200, json=NANO_USAGE)
        if "check-balance" in url:
            return httpx.Response(200, json={"usd_balance": "15.99945128", "nano_balance": "0"})
        if "deepseek.com/user/balance" in url:
            return httpx.Response(200, json=DS_BALANCE)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(upstream, "account_client", lambda: client)
    monkeypatch.setattr(rates, "_warned", {})

    class Harness:
        pass

    h = Harness()
    h.calls, h.override = calls, override
    return h


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ── the catalog ─────────────────────────────────────────────────────────────

async def test_the_catalog_is_filtered_to_the_models_in_use(vendors):
    await rates.refresh_catalogs()
    # nano: glm-5.3 and the flash model are in its allow-list; kimi-k3 is too but is not in the catalog.
    assert rates.card_for("nano", "z-ai/glm-5.3").base.input == pytest.approx(0.6)
    assert rates.card_for("nano", "deepseek/deepseek-v4.1-flash-1017:thinking") is not None
    assert rates.card_for("nano", "moonshotai/kimi-k3") is None
    assert rates.card_for("nano", "no/cache") is None, "a model nobody uses is not kept"
    # openrouter: glm-5.3 comes through the model_map and the logical model's target.
    assert rates.card_for("router", "z-ai/glm-5.3").base.input == pytest.approx(0.079)
    assert rates.card_for("router", "deepseek/deepseek-v4.1-flash-1017") is None


async def test_a_provider_without_a_vendor_is_never_called(vendors):
    await rates.refresh_catalogs()
    await rates.refresh_accounts()
    assert not any("box.lan" in url for _m, url, _h in vendors.calls)
    assert rates.card_for("local", "anything") is None


async def test_the_catalog_is_read_with_the_right_credentials(vendors):
    await rates.refresh_catalogs()
    await rates.refresh_accounts()
    by_url = {url: headers for _m, url, headers in vendors.calls}
    nano_models = next(h for u, h in by_url.items() if "nano-gpt.com/api/v1/models" in u)
    assert nano_models["authorization"] == "Bearer sk-nano-secret", "nano prices are the account's own"
    or_models = next(h for u, h in by_url.items() if "openrouter.ai/api/v1/models" in u)
    assert "authorization" not in or_models, "a public catalog is not sent a key"
    assert next(h for u, h in by_url.items() if "check-balance" in u)["x-api-key"] == "sk-nano-secret"
    methods = {url: m for m, url, _h in vendors.calls}
    assert methods[next(u for u in methods if "check-balance" in u)] == "POST"


async def test_the_operators_pricing_table_beats_the_fetched_catalog(vendors, monkeypatch):
    await rates.refresh_catalogs()
    table = Pricing(rates={"z-ai/glm-5.3": Rate(input=9.0, cached=1.0, output=18.0)})
    monkeypatch.setattr(conf.PROVIDERS_BY_NAME["nano"], "pricing", table)
    card = rates.card_for("nano", "z-ai/glm-5.3")
    assert card.source == "config" and card.base.input == 9.0
    # A model the table does not list still comes from the catalog.
    assert rates.card_for("nano", "deepseek/deepseek-v4.1-flash-1017:thinking").source == "list"


def test_a_config_table_carries_its_peak_window_as_overrides():
    table = Pricing(rates={"m": Rate(0.15, 0.003, 0.60)},
                    peak=Peak(multiplier=2, weekdays=frozenset(range(5)), hours=((1, 4), (6, 10))))
    card = rates._config_card(table, "m")
    assert card.at(utc(2026, 10, 21, 7, 30)).input == pytest.approx(0.30)     # Wednesday, peak
    assert card.at(utc(2026, 10, 21, 14, 0)).input == pytest.approx(0.15)
    assert card.at(utc(2026, 10, 24, 7, 30)).input == pytest.approx(0.15)     # Saturday
    assert card.at(utc(2026, 10, 21, 4, 0)).input == pytest.approx(0.15)      # end exclusive
    assert rates._config_card(table, "unlisted") is None


# ── a request's expected cost ───────────────────────────────────────────────

def test_expected_cost_weighs_the_rates_by_cache_and_output_share():
    card = RateCard(Rate(input=1.0, cached=0.1, output=4.0))
    now = utc(2026, 10, 21, 12)
    assert rates.expected_per_mtok(card, now, 0.0, 0.0) == pytest.approx(1.0)       # all uncached input
    assert rates.expected_per_mtok(card, now, 1.0, 0.0) == pytest.approx(0.1)       # all cache hits
    assert rates.expected_per_mtok(card, now, 0.0, 1.0) == pytest.approx(4.0)       # all output
    # 90% of prompt cached, 10% of tokens output: 0.9*(0.1*1.0 + 0.9*0.1) + 0.1*4.0
    assert rates.expected_per_mtok(card, now, 0.9, 0.1) == pytest.approx(0.9 * 0.19 + 0.4)


# ── the subscription ────────────────────────────────────────────────────────

def _quota(**kw):
    base = dict(used=10, limit=100, remaining=90, reset_at=1.0, allow_overage=True, available=True)
    base.update(kw)
    return Quota(**base)


def test_no_quota_means_no_opinion(vendors):
    assert rates.quota_state("nano", "z-ai/glm-5.3") is None


def test_a_covered_model_is_free_while_allowance_remains(vendors):
    rates._quotas["nano"] = _quota()
    rates._included["nano"] = frozenset({"z-ai/glm-5.3"})
    assert rates.quota_state("nano", "z-ai/glm-5.3") == "free"


def test_a_model_outside_the_subscription_bills_even_with_allowance_left(vendors):
    rates._quotas["nano"] = _quota()
    rates._included["nano"] = frozenset({"z-ai/glm-5.3"})
    assert rates.quota_state("nano", "moonshotai/kimi-k3") == "billed"


def test_an_unread_covered_list_assumes_the_model_is_covered(vendors):
    rates._quotas["nano"] = _quota()
    assert rates.quota_state("nano", "anything") == "free"


def test_once_the_allowance_is_gone_the_backend_bills_or_refuses(vendors):
    rates._included["nano"] = frozenset({"m"})
    rates._quotas["nano"] = _quota(remaining=0, available=False, allow_overage=True)
    assert rates.quota_state("nano", "m") == "billed"
    rates._quotas["nano"] = _quota(remaining=0, available=False, allow_overage=False)
    assert rates.quota_state("nano", "m") == "refused"


# ── the accounts ────────────────────────────────────────────────────────────

async def test_quota_and_balances_are_read_and_shown_without_a_key(vendors):
    await rates.refresh_accounts()
    snap = rates.accounts_snapshot()
    by = {a["name"]: a for a in snap["accounts"]}
    assert set(by) == {"nano", "router", "direct"}
    assert by["nano"]["quota"]["used"] == 58920726 and by["nano"]["quota"]["limit"] == 60000000
    assert by["nano"]["balance"] == {"currency": "USD", "amount": pytest.approx(15.99945128)}
    assert by["router"]["balance"]["amount"] == pytest.approx(17.724656236) and by["router"]["quota"] is None
    assert by["direct"]["balance"] == {"currency": "USD", "amount": 3.87}
    assert all(a["updated_at"] for a in by.values())
    text = str(snap)
    assert not any(secret in text for secret in ("sk-nano-secret", "sk-or-secret", "sk-ds-secret"))


async def test_a_failing_vendor_keeps_its_last_data_and_warns_once(vendors, caplog):
    await rates.refresh_accounts()
    await rates.refresh_catalogs()
    vendors.override["check-balance"] = lambda r: httpx.Response(500)
    vendors.override["subscription/v1/usage"] = lambda r: httpx.Response(503)
    vendors.override["openrouter.ai/api/v1/models"] = lambda r: httpx.Response(502)
    caplog.set_level(logging.WARNING, logger="llm-proxy")
    for _ in range(3):
        await rates.refresh_accounts()
        await rates.refresh_catalogs()
    snap = {a["name"]: a for a in rates.accounts_snapshot()["accounts"]}
    assert snap["nano"]["quota"]["used"] == 58920726, "the last good reading stands"
    assert snap["nano"]["balance"]["amount"] == pytest.approx(15.99945128)
    assert "quota" in snap["nano"]["error"] and "balance" in snap["nano"]["error"]
    assert rates.card_for("router", "z-ai/glm-5.3") is not None, "the last good catalog stands"
    warned = [r.getMessage() for r in caplog.records if "Rates: could not read" in r.getMessage()]
    assert len(warned) == 3, "one warning per provider and kind, not one per cycle"
    # Recovery clears the error.
    vendors.override.clear()
    await rates.refresh_accounts()
    assert rates.accounts_snapshot()["accounts"][0]["error"] is None


async def test_losing_the_subscription_clears_the_quota(vendors):
    await rates.refresh_accounts()
    assert rates.quota_for("nano") is not None
    vendors.override["subscription/v1/usage"] = lambda r: httpx.Response(200, json={"active": False})
    await rates.refresh_accounts()
    assert rates.quota_for("nano") is None


async def test_the_covered_models_are_fetched_with_the_catalog(vendors):
    await rates.refresh_catalogs()
    assert rates._included["nano"] == {"z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash-1017:thinking"}
    assert "router" not in rates._included


async def test_a_refresh_replaces_a_providers_cards_rather_than_adding_to_them(vendors):
    await rates.refresh_catalogs()
    assert rates.card_for("nano", "deepseek/deepseek-v4.1-flash-1017:thinking") is not None
    vendors.override["nano-gpt.com/api/v1/models"] = lambda r: httpx.Response(
        200, json={"data": [{"id": "z-ai/glm-5.3", "pricing": {"prompt": 0.5, "completion": 1.0}}]})
    await rates.refresh_catalogs()
    assert rates.card_for("nano", "z-ai/glm-5.3").base.input == 0.5
    assert rates.card_for("nano", "deepseek/deepseek-v4.1-flash-1017:thinking") is None


async def test_a_model_added_to_the_config_is_picked_up_without_waiting_an_hour(vendors, monkeypatch):
    before = rates._signature()
    monkeypatch.setattr(conf.PROVIDERS_BY_NAME["router"], "enabled_models", ["deepseek/deepseek-v4.1-flash-1017"])
    assert rates._signature() != before


async def test_a_cycle_reads_the_catalogs_first_then_only_when_due_or_changed(vendors, monkeypatch):
    state = await rates.cycle(None, None)
    assert rates.card_for("router", "z-ai/glm-5.3") is not None and rates.quota_for("nano") is not None
    catalog_calls = lambda: sum(1 for _m, u, _h in vendors.calls if "/api/v1/models" in u)
    first = catalog_calls()
    assert first == 2                                   # one catalog per vendor

    state = await rates.cycle(*state)                   # nothing due, nothing changed
    assert catalog_calls() == first
    assert sum(1 for _m, u, _h in vendors.calls if "check-balance" in u) == 2, "accounts are read every pass"

    monkeypatch.setattr(conf.PROVIDERS_BY_NAME["router"], "enabled_models", ["x/new-model"])
    state = await rates.cycle(*state)                   # a config change re-reads the catalogs at once
    assert catalog_calls() == first * 2

    state = await rates.cycle(state[0] - rates.CATALOG_EVERY - 1, state[1])   # an hour on
    assert catalog_calls() == first * 3
