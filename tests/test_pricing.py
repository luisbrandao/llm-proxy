"""The `pricing:` table: list prices for backends whose responses carry none.

DeepSeek's own API reports cache hits but no cost, so the proxy prices those
requests itself. The properties that matter: a cached token is billed at the
cache rate and never twice, the peak window is UTC and end-exclusive, a model
missing from the table stays unpriced, and a malformed table fails the load
instead of pricing everything at zero.
"""
from datetime import datetime, timezone

import pytest

from app import config as conf
from app.config import Peak, Pricing, Rate

FLASH = Rate(input=0.15, cached=0.003, output=0.60)
DEEPSEEK_PEAK = Peak(multiplier=2, weekdays=frozenset(range(5)), hours=((1, 4), (6, 10)))


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


WED_OFF = utc(2026, 10, 21, 14, 30)
WED_PEAK = utc(2026, 10, 21, 7, 30)


def test_cache_hits_are_billed_at_the_cache_rate_and_misses_at_the_input_rate():
    p = Pricing(rates={"deepseek-v4-flash": FLASH})
    # 1M prompt of which 900k cached, 100k out: 100k*0.15 + 900k*0.003 + 100k*0.60
    cost = p.price("deepseek-v4-flash", 1_000_000, 900_000, 100_000, WED_OFF)
    assert cost == pytest.approx(0.015 + 0.0027 + 0.06)


def test_a_cached_count_above_the_prompt_is_clamped_not_negative():
    p = Pricing(rates={"m": FLASH})
    assert p.price("m", 1000, 5000, 0, WED_OFF) == pytest.approx(1000 * 0.003 / 1e6)


def test_an_unlisted_model_stays_unpriced_unless_there_is_a_wildcard():
    assert Pricing(rates={"m": FLASH}).price("other", 10, 0, 10, WED_OFF) is None
    assert Pricing(rates={"*": FLASH}).price("other", 1_000_000, 0, 0, WED_OFF) == pytest.approx(0.15)


@pytest.mark.parametrize("when, peak", [
    (WED_PEAK, True),
    (utc(2026, 10, 21, 1, 0), True),     # window start is inclusive
    (utc(2026, 10, 21, 4, 0), False),    # window end is exclusive
    (utc(2026, 10, 21, 5, 59), False),   # the gap between the two windows
    (utc(2026, 10, 21, 9, 59), True),
    (utc(2026, 10, 24, 7, 30), False),   # Saturday
    (WED_OFF, False),
])
def test_the_peak_window_is_weekday_utc_hours(when, peak):
    p = Pricing(rates={"m": FLASH}, peak=DEEPSEEK_PEAK)
    base = 1_000_000 * 0.15 / 1e6
    assert p.price("m", 1_000_000, 0, 0, when) == pytest.approx(base * (2 if peak else 1))


def test_the_peak_window_reads_any_timezone_as_utc():
    from datetime import timedelta
    brt = timezone(timedelta(hours=-3))
    # 04:30 BRT on a Wednesday is 07:30 UTC — inside the window.
    assert DEEPSEEK_PEAK.applies(datetime(2026, 10, 21, 4, 30, tzinfo=brt))


def test_the_table_parses_from_config(load_config):
    load_config("""\
        providers:
          - name: deepseek
            base_url: "http://deepseek.invalid"
            pricing:
              models:
                deepseek-v4-flash: {input: 0.15, cached: 0.003, output: 0.60}
                plain: {input: 1, output: 2}
              peak:
                multiplier: 2
                weekdays: [mon, tue, wed, thu, fri]
                utc_hours: ["01-04", [6, 10]]
          - name: other
            base_url: "http://other.invalid"
        """)
    p = conf.PROVIDERS_BY_NAME["deepseek"].pricing
    assert p.rates["deepseek-v4-flash"] == FLASH
    assert p.rates["plain"] == Rate(input=1, cached=1, output=2), "cached defaults to the input rate"
    assert p.peak == DEEPSEEK_PEAK
    assert conf.PROVIDERS_BY_NAME["other"].pricing is None


@pytest.mark.parametrize("block", [
    "models: {m: {input: 1}}",                                  # no output rate
    "models: {m: {input: cheap, output: 1}}",                   # not a number
    "{models: {m: {input: 1, output: 1}}, peak: {utc_hours: ['04-01']}}",
    "{models: {m: {input: 1, output: 1}}, peak: {weekdays: [someday]}}",
])
def test_a_malformed_table_fails_the_load(tmp_path, block):
    text = f"""\
providers:
  - name: p
    base_url: "http://p.invalid"
    pricing: {block if block.startswith('{') else '{' + block + '}'}
"""
    with pytest.raises(ValueError, match="pricing"):
        conf._load(text.encode())
