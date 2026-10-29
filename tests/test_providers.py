"""The vendor fetchers' parsers, against payloads shaped like the real responses.

Each fixture below is a trimmed copy of what the live API returned on 2026-10-21 (see
docs/providers/). The things worth pinning are the units — OpenRouter prices per token,
NanoGPT per million but its cache price per thousand — and the time-of-day overrides,
because a unit slip silently misprices every backend by a factor of a thousand.
"""
from datetime import datetime, timezone

import pytest

from app import config as conf
from app.providers import Override, RateCard, hhmm_to_minutes, nanogpt, openrouter, deepseek

# DeepSeek's flash entry on OpenRouter, overrides exactly as served: weekdays at twice
# the base from 01:00 to 04:00 and from 06:00 to 10:00 UTC.
_BASE = {"prompt": "0.00000015", "completion": "0.0000006", "input_cache_read": "0.000000003"}
_PEAK = {"prompt": "0.0000003", "completion": "0.0000012", "input_cache_read": "0.000000006"}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
OPENROUTER_MODELS = {"data": [
    {"id": "deepseek/deepseek-v4.1-flash-1017", "canonical_slug": "deepseek/deepseek-v4.1-flash-1017",
     "pricing": {**_BASE, "overrides": [
         {"utc_days": ["saturday", "sunday"], **_BASE},
         {"utc_days": _WEEKDAYS, "utc_start": 0, "utc_end": 100, **_BASE},
         {"utc_days": _WEEKDAYS, "utc_start": 100, "utc_end": 400, **_PEAK},
         {"utc_days": _WEEKDAYS, "utc_start": 400, "utc_end": 600, **_BASE},
         {"utc_days": _WEEKDAYS, "utc_start": 600, "utc_end": 1000, **_PEAK},
         {"utc_days": _WEEKDAYS, "utc_start": 1000, "utc_end": 0, **_BASE},
     ]}},
    {"id": "z-ai/glm-5.3", "canonical_slug": "z-ai/glm-5.3-20261001",
     "pricing": {"prompt": "0.000000079", "completion": "0.0000014", "input_cache_read": "0.000000078"}},
    {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
    {"id": "free/model", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "broken/model", "pricing": {"prompt": "soon"}},
    {"id": "no/pricing"},
]}

NANO_MODELS = {"data": [
    {"id": "deepseek/deepseek-v4.1-flash-1017:thinking",
     "pricing": {"prompt": 0.09999999999999999, "completion": 0.6, "cacheReadInputPer1kTokens": 3e-06,
                 "currency": "USD", "unit": "per_million_tokens"}},
    {"id": "z-ai/glm-5.3", "pricing": {"prompt": 0.6, "completion": 1.1, "cacheReadInputPer1kTokens": 0.0002}},
    {"id": "no/cache", "pricing": {"prompt": 2, "completion": 8}},
    {"id": "no/pricing"},
]}

NANO_USAGE = {
    "active": True, "state": "active", "allowOverage": True,
    "limits": {"weeklyInputTokens": 60000000, "dailyInputTokens": None, "dailyImages": 100},
    "weeklyInputTokens": {"used": 58920726, "remaining": 1079274, "percentUsed": 0.9820121,
                          "resetAt": 1792972800000},
    "dailyInputTokens": None,
    "routing": {"recommendedMode": "subscription", "reason": "included_quota_available",
                "billingMode": "both", "paidOverageEnabled": True},
}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ── OpenRouter ──────────────────────────────────────────────────────────────

def test_openrouter_prices_are_converted_from_per_token_to_per_million():
    cards = openrouter.parse_models(OPENROUTER_MODELS)
    glm = cards["z-ai/glm-5.3"].base
    assert (glm.input, glm.cached, glm.output) == pytest.approx((0.079, 0.078, 1.4))
    flash = cards["deepseek/deepseek-v4.1-flash-1017"].base
    assert (flash.input, flash.cached, flash.output) == pytest.approx((0.15, 0.003, 0.60))


def test_a_model_is_reachable_by_its_canonical_slug_too():
    cards = openrouter.parse_models(OPENROUTER_MODELS)
    assert cards["z-ai/glm-5.3-20261001"] is cards["z-ai/glm-5.3"]


def test_unpriceable_entries_are_skipped_and_free_is_free():
    cards = openrouter.parse_models(OPENROUTER_MODELS)
    assert "openrouter/auto" not in cards, "a negative price means 'depends on the routed model'"
    assert "broken/model" not in cards and "no/pricing" not in cards
    assert cards["free/model"].base.input == 0.0


@pytest.mark.parametrize("when, factor", [
    (utc(2026, 10, 21, 7, 30), 2),    # Wednesday inside 06:00 to 10:00
    (utc(2026, 10, 21, 2, 0), 2),     # Wednesday inside 01:00 to 04:00
    (utc(2026, 10, 21, 1, 0), 2),     # window start is inclusive
    (utc(2026, 10, 21, 4, 0), 1),     # window end is exclusive
    (utc(2026, 10, 21, 5, 59), 1),    # the gap between the two windows
    (utc(2026, 10, 21, 10, 0), 1),    # second window ends at 10:00
    (utc(2026, 10, 21, 14, 30), 1),   # afternoon: 10:00 to midnight is base
    (utc(2026, 10, 21, 0, 30), 1),
    (utc(2026, 10, 24, 7, 30), 1),    # Saturday
    (utc(2026, 10, 25, 2, 0), 1),     # Sunday
])
def test_openrouter_time_of_day_overrides_double_the_weekday_windows(when, factor):
    card = openrouter.parse_models(OPENROUTER_MODELS)["deepseek/deepseek-v4.1-flash-1017"]
    r = card.at(when)
    assert (r.input, r.cached, r.output) == pytest.approx((0.15 * factor, 0.003 * factor, 0.60 * factor))


def test_an_override_that_depends_on_prompt_length_is_left_out():
    """It cannot be evaluated without the request, so it is not guessed at."""
    payload = {"data": [{"id": "long/context", "pricing": {
        "prompt": "0.000001", "completion": "0.000002",
        "overrides": [{"min_prompt_tokens": 200000, "prompt": "0.000005", "completion": "0.00002"}]}}]}
    card = openrouter.parse_models(payload)["long/context"]
    assert card.overrides == ()
    assert card.at(utc(2026, 10, 21, 12)).input == pytest.approx(1.0)


def test_a_window_that_runs_past_midnight_wraps():
    o = Override(prices=(("input", 9.0),), start=hhmm_to_minutes(2200), end=hhmm_to_minutes(600))
    assert o.applies(utc(2026, 10, 21, 23, 0)) and o.applies(utc(2026, 10, 21, 3, 0))
    assert not o.applies(utc(2026, 10, 21, 12, 0))


def test_later_overrides_win_per_key_and_omitted_keys_keep_the_base():
    from app.config import Rate
    card = RateCard(Rate(1.0, 0.1, 2.0), overrides=(
        Override(prices=(("input", 5.0), ("output", 6.0))),
        Override(prices=(("input", 7.0),)),
    ))
    r = card.at(utc(2026, 10, 21, 12))
    assert (r.input, r.cached, r.output) == (7.0, 0.1, 6.0)


def test_openrouter_credits_are_what_is_left():
    b = openrouter.parse_credits({"data": {"total_credits": 270, "total_usage": 252.275343764}})
    assert b.currency == "USD" and b.amount == pytest.approx(17.724656236)
    assert openrouter.parse_credits({"data": {}}) is None
    assert openrouter.parse_credits(None) is None


# ── NanoGPT ─────────────────────────────────────────────────────────────────

def test_nano_cache_price_is_per_thousand_while_the_rest_is_per_million():
    cards = nanogpt.parse_models(NANO_MODELS)
    flash = cards["deepseek/deepseek-v4.1-flash-1017:thinking"].base
    assert (flash.input, flash.cached, flash.output) == pytest.approx((0.1, 0.003, 0.6))
    assert cards["z-ai/glm-5.3"].base.cached == pytest.approx(0.2)


def test_a_nano_model_without_a_cache_price_is_billed_cache_hits_at_the_input_rate():
    assert nanogpt.parse_models(NANO_MODELS)["no/cache"].base.cached == 2.0
    assert "no/pricing" not in nanogpt.parse_models(NANO_MODELS)


def test_the_quota_is_read_from_the_real_response_shape():
    q = nanogpt.parse_quota(NANO_USAGE)
    assert (q.used, q.limit, q.remaining) == (58920726, 60000000, 1079274)
    assert q.reset_at == 1792972800 and q.allow_overage and q.available and q.mode == "subscription"
    assert q.percent == pytest.approx(0.982, abs=1e-3)


def test_an_exhausted_quota_is_not_available_when_nano_says_paygo():
    exhausted = {**NANO_USAGE, "weeklyInputTokens": {"used": 60000100, "remaining": 0, "resetAt": 1792972800000},
                 "routing": {"recommendedMode": "paygo"}}
    q = nanogpt.parse_quota(exhausted)
    assert q.remaining == 0 and q.available is False and q.mode == "paygo"


def test_a_stale_verdict_cannot_make_an_empty_allowance_available():
    """The payload NanoGPT actually returned on 2026-10-21 14:16 BRT: the allowance was gone
    and every request was already being billed, yet the verdict still said `subscription`."""
    observed = {**NANO_USAGE,
                "weeklyInputTokens": {"used": 60001719, "remaining": 0, "percentUsed": 1.00002865,
                                      "resetAt": 1792972800000},
                "routing": {"recommendedMode": "subscription", "reason": "included_quota_available",
                            "subscriptionQuotaAvailable": True, "paidOverageEnabled": True}}
    q = nanogpt.parse_quota(observed)
    assert q.remaining == 0 and q.used > q.limit
    assert q.available is False


def test_the_verdict_can_still_veto_an_allowance_that_has_tokens_left():
    q = nanogpt.parse_quota({**NANO_USAGE, "routing": {"recommendedMode": "paygo"}})
    assert q.remaining > 0 and q.available is False


def test_without_a_verdict_available_means_enough_is_left():
    bare = {**NANO_USAGE, "routing": {}}
    assert nanogpt.parse_quota(bare).available is True
    bare["weeklyInputTokens"] = {"used": 60000000, "remaining": 0, "resetAt": 1792972800000}
    assert nanogpt.parse_quota(bare).available is False


def test_a_counter_parked_just_under_the_limit_is_used_up():
    """What NanoGPT returned on 2026-10-28 from 19:04 BRT on: a request that does not fit
    is billed and not counted, so the counter stopped 821 tokens short of the limit, the
    verdict still said `subscription`, and every request was billed. 821 tokens is no
    allowance: no request is that small."""
    parked = {**NANO_USAGE,
              "weeklyInputTokens": {"used": 59999179, "remaining": 821, "resetAt": 1793577600000},
              "routing": {"recommendedMode": "subscription", "reason": "included_quota_available"}}
    q = nanogpt.parse_quota(parked)
    assert q.remaining == 821 and q.available is False


@pytest.mark.parametrize("remaining,available", [
    (599_999, False), (600_000, True), (1_079_274, True),
])
def test_the_allowance_counts_as_left_from_one_percent_of_the_limit(remaining, available):
    counter = {"used": 60_000_000 - remaining, "remaining": remaining, "resetAt": 1793577600000}
    assert nanogpt.parse_quota({**NANO_USAGE, "weeklyInputTokens": counter}).available is available


def test_the_tighter_of_the_daily_and_weekly_counters_rules():
    both = {**NANO_USAGE, "limits": {"weeklyInputTokens": 60000000, "dailyInputTokens": 5000000},
            "dailyInputTokens": {"used": 4900000, "remaining": 100000, "resetAt": 1792627200000}}
    q = nanogpt.parse_quota(both)
    assert (q.limit, q.remaining, q.reset_at) == (5000000, 100000, 1792627200)


@pytest.mark.parametrize("payload", [None, {}, {"active": False}, {"active": True},
                                     {"active": True, "weeklyInputTokens": {"used": None}}])
def test_no_subscription_or_no_counter_is_no_quota(payload):
    assert nanogpt.parse_quota(payload) is None


def test_the_covered_models_and_the_balance():
    assert nanogpt.parse_included({"data": [{"id": "a"}, {"id": "b"}, {"nope": 1}]}) == {"a", "b"}
    b = nanogpt.parse_balance({"usd_balance": "15.99945128", "nano_balance": "0.00000000"})
    assert b.currency == "USD" and b.amount == pytest.approx(15.99945128)
    assert nanogpt.parse_balance({}) is None


# ── DeepSeek ────────────────────────────────────────────────────────────────

def test_deepseek_balance_prefers_the_usd_wallet():
    payload = {"is_available": True, "balance_infos": [
        {"currency": "CNY", "total_balance": "110.00"}, {"currency": "USD", "total_balance": "3.87"}]}
    b = deepseek.parse_balance(payload)
    assert (b.currency, b.amount) == ("USD", 3.87)


def test_deepseek_balance_falls_back_to_whatever_it_holds():
    b = deepseek.parse_balance({"balance_infos": [{"currency": "CNY", "total_balance": "110.00"}]})
    assert (b.currency, b.amount) == ("CNY", 110.0)
    assert deepseek.parse_balance({"balance_infos": []}) is None
    assert deepseek.parse_balance({}) is None


# ── which vendor a backend is ───────────────────────────────────────────────

@pytest.mark.parametrize("url, vendor", [
    ("https://openrouter.ai/api", "openrouter"),
    ("https://nano-gpt.com/api", "nanogpt"),
    ("https://api.nano-gpt.com/api/", "nanogpt"),
    ("https://api.deepseek.com", "deepseek"),
    ("http://citrine.brandao:11434", ""),
    ("https://generativelanguage.googleapis.com/v1beta/openai", ""),
])
def test_the_vendor_is_detected_from_the_host(url, vendor):
    assert conf.vendor_of(conf.Provider(name="p", base_url=url)) == vendor


def test_an_explicit_vendor_wins_over_the_host():
    assert conf.vendor_of(conf.Provider(name="p", base_url="http://proxy.lan/or", vendor="openrouter")) == "openrouter"


def test_a_mistyped_vendor_fails_the_load(load_config):
    with pytest.raises(ValueError, match="vendor"):
        load_config("providers:\n  - {name: a, base_url: 'http://a.invalid', vendor: opnrouter}\n")
    load_config("providers:\n  - {name: a, base_url: 'http://a.invalid', vendor: OpenRouter}\n")
    assert conf.PROVIDERS_BY_NAME["a"].vendor == "openrouter"


# ── DeepSeek: reasoning has to come back under its own name ─────────────────

def _tool_turn(**extra):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{}"}}], **extra}


def _chat(*messages):
    return {"model": "deepseek-flash", "messages": list(messages), "stream": True}


USER = {"role": "user", "content": "weather?"}
TOOL = {"role": "tool", "tool_call_id": "c1", "content": "22C"}


def test_a_tool_turn_from_nano_gets_its_reasoning_under_deepseeks_name():
    """The case that killed opencode: nano wrote the turn, so it says `reasoning`."""
    payload = _chat(USER, _tool_turn(reasoning="I should call the weather tool."), TOOL)
    out = deepseek.adapt_request(payload)
    assert out["messages"][1]["reasoning_content"] == "I should call the weather tool."
    assert out["messages"][1]["reasoning"] == "I should call the weather tool.", "nothing is removed"


def test_openrouters_structured_reasoning_is_carried_across_too():
    turn = _tool_turn(reasoning_details=[{"type": "reasoning.text", "text": "Need weather. "},
                                         {"type": "reasoning.text", "text": "Call the tool."}])
    out = deepseek.adapt_request(_chat(USER, turn, TOOL))
    assert out["messages"][1]["reasoning_content"] == "Need weather. Call the tool."


def test_a_turn_that_carries_no_reasoning_at_all_still_gets_the_key():
    """DeepSeek checks the key, not its content: an empty string is accepted."""
    out = deepseek.adapt_request(_chat(USER, _tool_turn(), TOOL))
    assert out["messages"][1]["reasoning_content"] == ""


def test_a_turn_that_already_has_the_key_is_left_alone_even_when_it_is_empty():
    payload = _chat(USER, _tool_turn(reasoning_content=""), TOOL)
    assert deepseek.adapt_request(payload) is payload
    payload = _chat(USER, _tool_turn(reasoning_content="kept", reasoning="other"), TOOL)
    assert deepseek.adapt_request(payload) is payload


def test_only_tool_call_assistant_turns_are_touched():
    plain = {"role": "assistant", "content": "Hi!", "reasoning": "greeting"}
    payload = _chat({"role": "user", "content": "hi"}, plain, USER, _tool_turn(reasoning="r"), TOOL)
    out = deepseek.adapt_request(payload)
    assert out["messages"][1] == plain, "plain answers are not checked by DeepSeek"
    assert out["messages"][0] is payload["messages"][0] and out["messages"][4] is payload["messages"][4]
    assert out["messages"][3]["reasoning_content"] == "r"


def test_every_tool_turn_in_a_long_conversation_is_repaired():
    payload = _chat(USER, _tool_turn(reasoning="one"), TOOL, _tool_turn(reasoning="two"), TOOL)
    out = deepseek.adapt_request(payload)
    assert [m.get("reasoning_content") for m in out["messages"] if m["role"] == "assistant"] == ["one", "two"]


def test_the_shared_payload_is_never_mutated():
    """It is also what the failover targets still to be tried will send."""
    turn = _tool_turn(reasoning="r")
    payload = _chat(USER, turn, TOOL)
    out = deepseek.adapt_request(payload)
    assert out is not payload and "reasoning_content" not in turn and "reasoning_content" not in payload["messages"][1]


@pytest.mark.parametrize("payload", [
    {"model": "m"}, {"model": "m", "messages": None}, {"model": "m", "messages": "text"},
    {"model": "m", "messages": [None, 3, "x", {"role": "assistant"}, {"role": "assistant", "tool_calls": []}]},
])
def test_a_malformed_body_passes_through_untouched(payload):
    assert deepseek.adapt_request(payload) is payload
