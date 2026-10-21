"""Smart routing: cheapest backend first, and the operator's override.

`smart.order` is pure — no await, no I/O, only a read of the price snapshot — so it
is driven directly with hand-built targets. The properties worth pinning are the
ones a reorder could silently get wrong: that nothing the router cannot price is
ever moved (an unknown price must not read as cheap), that a tie tier stays a tie
tier, that `first`/`last` really are strict, and that the whole thing is inert
unless switched on. `test_proxy.py` closes the loop through the real lifecycle.
"""
import dataclasses
import logging

import pytest

from app import config as conf
from app import ledger, smart
from app.usage import Usage

PROVIDERS = [{"name": n, "base_url": f"http://{n}.invalid/v1"} for n in ("a", "b", "c", "d")]


def targets(*pairs):
    """[(provider, priority), ...] as the resolver would hand them over."""
    return [conf.Target(provider=p, model=f"native-{p}", priority=prio) for p, prio in pairs]


def names(ts):
    return [t.provider for t in ts]


def prices(**by_provider):
    """Install a price snapshot for the model 'm': provider -> $/M tokens (n=10)."""
    smart._prices = {("m", p): (price, 10) for p, price in by_provider.items()}


@pytest.fixture
def smart_on(providers):
    def _on(*extra, **routing):
        specs = list(PROVIDERS) + list(extra)
        routing.setdefault("smart_routing", True)
        routing.setdefault("smart_min_requests", 5)
        return providers(*specs, **routing)

    return _on


def test_it_is_inert_unless_switched_on(smart_on):
    smart_on(smart_routing=False)
    prices(a=3.0, b=1.0)
    out, note = smart.order(targets(("a", 1), ("b", 2)), "m")
    assert names(out) == ["a", "b"] and note is None


def test_cheapest_first_when_on(smart_on):
    smart_on()
    prices(a=3.0, b=1.0, c=2.0)
    out, note = smart.order(targets(("a", 1), ("b", 2), ("c", 3)), "m")
    assert names(out) == ["b", "c", "a"]
    assert note["by"] == "smart" and note["to"] == "b"
    assert note["prices"] == {"b": 1.0, "c": 2.0, "a": 3.0}


def test_positions_keep_their_priority_values_so_a_tie_tier_stays_a_tie_tier(smart_on):
    """a and b are tied at priority 1 (round-robined by the slot picker), c is the
    fallback at 2. Ranking must reorder who sits where, never the tier shape."""
    smart_on()
    prices(a=3.0, b=1.0, c=2.0)
    out, _ = smart.order(targets(("a", 1), ("b", 1), ("c", 2)), "m")
    assert [(t.provider, t.priority) for t in out] == [("b", 1), ("c", 1), ("a", 2)]


def test_a_backend_with_no_price_is_never_moved(smart_on):
    """Unknown is not cheap: `a` has no data, so it keeps position 0 and only the
    two it can price trade places."""
    smart_on()
    prices(b=2.0, c=1.0)
    out, _ = smart.order(targets(("a", 1), ("b", 2), ("c", 3)), "m")
    assert names(out) == ["a", "c", "b"]


def test_too_few_priced_requests_means_unranked(smart_on):
    smart_on(smart_min_requests=5)
    smart._prices = {("m", "a"): (9.0, 10), ("m", "b"): (0.1, 4), ("m", "c"): (1.0, 10)}
    out, _ = smart.order(targets(("a", 1), ("b", 2), ("c", 3)), "m")
    # b is cheapest but only 4 samples: it stays at 2; a and c are ranked into 1 and 3.
    assert names(out) == ["c", "b", "a"]


def test_a_single_rankable_backend_changes_nothing(smart_on):
    smart_on()
    prices(a=5.0)
    out, note = smart.order(targets(("a", 1), ("b", 2)), "m")
    assert names(out) == ["a", "b"] and note is None


def test_equal_prices_keep_the_configured_order(smart_on):
    """The local boxes are priced at 0, so they tie — and a tie must not shuffle them."""
    smart_on()
    prices(a=0.0, b=0.0, c=0.0)
    out, note = smart.order(targets(("a", 1), ("b", 2), ("c", 3)), "m")
    assert names(out) == ["a", "b", "c"] and note is None


def test_free_backends_rank_ahead_of_paid_ones(smart_on):
    smart_on()
    prices(a=2.0, b=0.0)
    out, _ = smart.order(targets(("a", 1), ("b", 2)), "m")
    assert names(out) == ["b", "a"]


def test_prices_are_per_model(smart_on):
    smart_on()
    smart._prices = {("other", "a"): (9.0, 10), ("other", "b"): (1.0, 10)}
    out, note = smart.order(targets(("a", 1), ("b", 2)), "m")
    assert names(out) == ["a", "b"] and note is None


def test_a_fixed_backend_stays_put(smart_on):
    smart_on(backend_modes={"a": "fixed"})
    prices(a=9.0, b=2.0, c=1.0)
    out, _ = smart.order(targets(("a", 1), ("b", 2), ("c", 3)), "m")
    assert names(out) == ["a", "c", "b"]


def test_a_subscription_seat_is_always_fixed(smart_on):
    """The claude CLI draws from a seat: no per-request price, and not a cost decision."""
    smart_on({"name": "seat", "kind": conf.KIND_CLAUDE_CLI, "base_url": ""})
    prices(a=9.0, b=1.0, seat=0.01)
    assert smart.mode_of("seat") == "fixed"
    out, _ = smart.order(targets(("a", 1), ("seat", 2), ("b", 3)), "m")
    assert names(out) == ["b", "seat", "a"]


def test_first_and_last_are_strict_even_with_smart_routing_off(smart_on):
    smart_on(smart_routing=False, backend_modes={"c": "first", "a": "last"})
    out, note = smart.order(targets(("a", 1), ("b", 1), ("c", 1)), "m")
    assert [(t.provider, t.priority) for t in out] == [("c", 1), ("b", 2), ("a", 3)]
    assert note["by"] == "pinned" and note["to"] == "c"


def test_an_override_beats_the_cost_ranking(smart_on):
    smart_on(backend_modes={"a": "first"})
    prices(a=9.0, b=1.0, c=2.0)
    out, note = smart.order(targets(("a", 1), ("b", 2), ("c", 3)), "m")
    assert names(out) == ["a", "b", "c"] and note["by"] == "pinned"


def test_an_overridden_backend_is_not_part_of_the_ranking(smart_on):
    smart_on(backend_modes={"d": "last"})
    prices(a=3.0, b=1.0, c=2.0, d=0.01)
    out, _ = smart.order(targets(("a", 1), ("b", 2), ("c", 3), ("d", 4)), "m")
    assert names(out) == ["b", "c", "a", "d"]


def test_an_override_for_a_backend_this_model_does_not_use_is_inert(smart_on):
    smart_on(smart_routing=False, backend_modes={"d": "first"})
    out, note = smart.order(targets(("a", 1), ("b", 2)), "m")
    assert names(out) == ["a", "b"] and note is None


def test_a_single_target_is_returned_untouched(smart_on):
    smart_on(backend_modes={"a": "first"})
    out, note = smart.order(targets(("a", 1)), "m")
    assert names(out) == ["a"] and note is None


def test_the_input_targets_are_never_mutated(smart_on):
    """Targets are shared with the live config; a reorder works on copies."""
    smart_on(backend_modes={"c": "first"})
    prices(a=3.0, b=1.0)
    original = targets(("a", 1), ("b", 2), ("c", 3))
    before = [(t.provider, t.priority) for t in original]
    smart.order(original, "m")
    assert [(t.provider, t.priority) for t in original] == before


# ── config ──────────────────────────────────────────────────────────────────

def test_the_routing_block_parses_and_defaults_are_off(load_config):
    load_config("providers:\n  - {name: a, base_url: 'http://a.invalid'}\n")
    assert conf.ROUTING.smart_routing is False and conf.ROUTING.backend_modes == {}
    load_config("""\
        providers:
          - {name: a, base_url: 'http://a.invalid'}
        routing:
          smart_routing: true
          smart_window_days: 14
          smart_min_requests: 3
          backend_modes: {a: First, google: fixed}
        """)
    r = conf.ROUTING
    assert (r.smart_routing, r.smart_window_days, r.smart_min_requests) == (True, 14, 3)
    assert r.backend_modes == {"a": "first", "google": "fixed"}


def test_a_mistyped_mode_fails_the_load_instead_of_being_ignored(load_config):
    with pytest.raises(ValueError, match="backend_modes"):
        load_config("""\
            providers:
              - {name: a, base_url: 'http://a.invalid'}
            routing:
              backend_modes: {a: frist}
            """)


# ── the price snapshot ──────────────────────────────────────────────────────

async def _row(**kw):
    row = dict(provider="a", model="n", asked="m", served=None, status=200, stream=False,
               op=None, duration=1.0, usage=Usage(prompt=900_000, completion=100_000, cost=2.0, currency="USD"),
               svc=None, client=None)
    row.update(kw)
    await ledger.record(**row)


async def test_observed_prices_are_blended_dollars_per_million_tokens():
    await _row()
    await _row(usage=Usage(prompt=1_800_000, completion=200_000, cost=6.0, currency="USD"))
    got = await ledger.observed_prices(7)
    assert got == {("m", "a"): (pytest.approx(8.0 / 3.0), 2)}   # $8 over 3M tokens


async def test_only_successful_priced_requests_with_tokens_count():
    await _row()
    await _row(status=503, usage=Usage(prompt=5, completion=5, cost=99.0, currency="USD"))
    await _row(usage=Usage(prompt=5, completion=5))                      # unpriced
    await _row(usage=Usage(cost=1.0, currency="USD"))                    # no tokens
    got = await ledger.observed_prices(7)
    assert got == {("m", "a"): (pytest.approx(2.0), 1)}


async def test_prices_are_per_real_provider_not_per_ollama_group():
    await _row(provider="ollamaGW", usage=Usage(prompt=1_000_000, cost=0.0, currency="USD"))
    await _row(provider="ollamaLuis", usage=Usage(prompt=1_000_000, cost=0.0, currency="USD"))
    got = await ledger.observed_prices(7)
    assert set(got) == {("m", "ollamaGW"), ("m", "ollamaLuis")}


async def test_the_window_bounds_what_is_read():
    from datetime import datetime, timedelta
    await _row(when=datetime.now().astimezone() - timedelta(days=10))
    assert await ledger.observed_prices(7) == {}
    assert len(await ledger.observed_prices(30)) == 1


async def test_refresh_installs_the_snapshot(providers):
    providers(*PROVIDERS)
    await _row()
    await smart.refresh()
    assert ("m", "a") in smart._prices and smart._refreshed_at is not None


async def test_a_failed_refresh_keeps_the_last_snapshot_and_never_raises(monkeypatch, caplog):
    smart._prices = {("m", "a"): (1.0, 10)}

    async def boom(days):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(ledger, "observed_prices", boom)
    monkeypatch.setattr(smart, "_warned_at", None)
    caplog.set_level(logging.WARNING, logger="llm-proxy")
    await smart.refresh()
    await smart.refresh()
    assert smart._prices == {("m", "a"): (1.0, 10)}
    assert len([r for r in caplog.records if "Smart routing" in r.getMessage()]) == 1


def test_the_snapshot_shows_each_models_order_now_against_its_configured_one(smart_on, monkeypatch):
    smart_on(backend_modes={"d": "fixed"})
    monkeypatch.setattr(conf, "LOGICAL_MODELS", {
        "m": conf.LogicalModel("m", [conf.Target("a", None, 1), conf.Target("b", None, 2)]),
    })
    prices(a=3.0, b=1.0)
    snap = smart.snapshot()
    assert snap["enabled"] is True and snap["min_requests"] == 5
    assert snap["models"] == [{"name": "m", "static": ["a", "b"], "current": ["b", "a"], "by": "smart"}]
    assert {"model": "m", "provider": "b", "usd_per_mtok": 1.0, "requests": 10} in snap["prices"]
    modes = {p["name"]: p["mode"] for p in snap["providers"]}
    assert modes["d"] == "fixed" and modes["a"] == "auto"
