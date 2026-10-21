"""The cost ledger (`app/ledger.py`), driven directly.

What matters here is the shape the Costs tab reads back — totals, per-day,
per-model with the per-provider split, per-provider, the request list — and
the two safety properties: a disabled or broken ledger never touches a request,
and old rows are pruned rather than kept forever. `test_proxy.py` covers the
wire end: a priced response produces a row.
"""
import logging
from datetime import datetime, timedelta

import pytest

from app import config as conf
from app import ledger
from app.usage import Usage


def ago(days: int) -> datetime:
    return datetime.now().astimezone() - timedelta(days=days)


async def add(**overrides):
    row = dict(
        provider="nano", model="z/glm", asked="glm", served=None, status=200, stream=False,
        op=None, duration=1.5, usage=Usage(prompt=100, completion=10), svc="OpenWebUI",
        client="desk.lan",
    )
    row.update(overrides)
    await ledger.record(**row)


async def test_rows_aggregate_by_model_day_and_provider():
    await add(usage=Usage(prompt=100, completion=10, cached=50, cost=0.001, currency="USD"))
    await add(provider="router", model="zz/glm", usage=Usage(prompt=200, completion=20, cost=0.002, currency="USD"))
    await add(provider="ollama", model="free", asked="free", usage=Usage(prompt=10, completion=1))
    s = await ledger.summary(days=30)

    assert s["enabled"] is True and s["rows"] == 3
    t = s["totals"]
    assert (t["requests"], t["errors"], t["priced"]) == (3, 0, 2)
    assert t["cost"] == pytest.approx(0.003)
    assert (t["prompt"], t["completion"], t["cached"]) == (310, 31, 50)

    # Most expensive model first, with its split across the backends that served it.
    glm, free = s["by_model"]
    assert glm["model"] == "glm" and glm["requests"] == 2 and glm["cost"] == pytest.approx(0.003)
    split = {(p["provider"], p["model"]): p["cost"] for p in glm["providers"]}
    assert split == {("router", "zz/glm"): pytest.approx(0.002), ("nano", "z/glm"): pytest.approx(0.001)}
    assert free["model"] == "free" and free["priced"] == 0 and free["cost"] == 0

    assert [d["day"] for d in s["by_day"]] == [ledger.local_day()]
    assert s["by_day"][0]["requests"] == 3
    assert {p["provider"] for p in s["by_provider"]} == {"nano", "router", "ollama"}

    # Newest first, every field the console reads.
    assert [r["asked"] for r in s["requests"]] == ["free", "glm", "glm"]
    newest = s["requests"][0]
    assert newest["provider"] == "ollama" and newest["model"] == "free" and newest["cost"] is None
    assert newest["client"] == "desk.lan" and newest["svc"] == "OpenWebUI" and newest["at"]
    assert s["models"] == ["free", "glm"] and s["providers"] == ["nano", "ollama", "router"]
    assert s["currencies"] == ["USD"]


async def test_the_served_model_is_kept_only_when_it_differs():
    await add(served="z/glm")
    await add(served="z/glm-2026-09")
    s = await ledger.summary()
    assert [r["served"] for r in s["requests"]] == ["z/glm-2026-09", None]


async def test_failures_count_as_requests_but_never_as_priced():
    await add(status=503, usage=Usage(), duration=0.2)
    await add(usage=Usage(prompt=5, completion=5, cost=0.5, currency="USD"))
    t = (await ledger.summary())["totals"]
    assert (t["requests"], t["errors"], t["priced"]) == (2, 1, 1)
    assert t["cost"] == pytest.approx(0.5)


async def test_a_day_filter_narrows_everything_but_the_day_series():
    yesterday = ledger.local_day(ago(1))
    await add(when=ago(1), usage=Usage(prompt=1, completion=1, cost=1.0, currency="USD"))
    await add(usage=Usage(prompt=1, completion=1, cost=2.0, currency="USD"))
    await add(usage=Usage(prompt=1, completion=1, cost=4.0, currency="USD"))

    s = await ledger.summary(days=7, day=yesterday)
    assert s["range"]["day"] == yesterday
    assert s["totals"]["requests"] == 1 and s["totals"]["cost"] == pytest.approx(1.0)
    assert len(s["requests"]) == 1 and s["requests"][0]["day"] == yesterday
    assert s["by_model"][0]["cost"] == pytest.approx(1.0)
    # The chart keeps the whole window so another day can be picked from it.
    assert [d["requests"] for d in s["by_day"]] == [1, 2]


async def test_the_range_is_counted_in_local_days():
    await add(when=ago(10), usage=Usage(prompt=1, completion=1, cost=1.0, currency="USD"))
    await add(usage=Usage(prompt=1, completion=1, cost=2.0, currency="USD"))
    week = await ledger.summary(days=7)
    assert week["totals"]["requests"] == 1 and week["range"]["since"] == ledger.local_day(ago(6))
    assert week["models"] == ["glm"]
    everything = await ledger.summary(days=0)
    assert everything["totals"]["requests"] == 2 and everything["range"]["since"] is None
    today = await ledger.summary(days=1)
    assert today["range"]["since"] == ledger.local_day() and today["totals"]["requests"] == 1


async def test_model_and_provider_filters():
    await add(asked="a", provider="p1")
    await add(asked="a", provider="p2")
    await add(asked="b", provider="p1")
    s = await ledger.summary(model="a")
    assert s["totals"]["requests"] == 2 and s["filters"] == {"model": "a", "provider": None}
    # The dropdowns keep offering every option in range, not just the filtered one.
    assert s["models"] == ["a", "b"] and s["providers"] == ["p1", "p2"]
    s = await ledger.summary(model="a", provider="p2")
    assert s["totals"]["requests"] == 1 and s["requests"][0]["provider"] == "p2"
    s = await ledger.summary(provider="p1")
    assert [m["model"] for m in s["by_model"]] == ["a", "b"] or [m["model"] for m in s["by_model"]] == ["b", "a"]
    assert s["totals"]["requests"] == 2


async def test_the_request_list_is_capped_but_the_totals_are_not():
    for _ in range(5):
        await add()
    s = await ledger.summary(limit=2)
    assert len(s["requests"]) == 2 and s["totals"]["requests"] == 5 and s["limit"] == 2


async def test_rows_past_the_retention_window_are_pruned_on_reopen(tmp_path, monkeypatch):
    # A file, not :memory: — closing a memory ledger discards it, and the prune
    # under test runs on reopen.
    monkeypatch.setattr(conf, "LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setattr(conf, "LEDGER_RETENTION_DAYS", 30)
    await add(when=ago(45))
    await add(when=ago(29))
    await add()
    assert (await ledger.summary(days=0))["rows"] == 3
    # The prune runs when the connection opens (startup) and once a day after.
    ledger.close()
    s = await ledger.summary(days=0)
    assert s["rows"] == 2 and s["oldest"] == ledger.local_day(ago(29))


async def test_zero_retention_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setattr(conf, "LEDGER_RETENTION_DAYS", 0)
    await add(when=ago(5000))
    ledger.close()
    assert (await ledger.summary(days=0))["rows"] == 1


async def test_a_file_ledger_survives_a_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    await add(usage=Usage(prompt=1, completion=1, cost=0.75, currency="USD"))
    ledger.close()
    s = await ledger.summary()
    assert s["rows"] == 1 and s["totals"]["cost"] == pytest.approx(0.75)
    assert s["path"] == str(tmp_path / "ledger.sqlite")


async def test_a_disabled_ledger_records_nothing_and_says_so(monkeypatch):
    monkeypatch.setattr(conf, "LEDGER_PATH", "")
    await add()
    s = await ledger.summary()
    assert s == {"enabled": False, "path": "", "retention_days": conf.LEDGER_RETENTION_DAYS}


async def test_a_ledger_that_cannot_be_written_never_raises(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(conf, "LEDGER_PATH", str(tmp_path / "missing" / "dir" / "ledger.sqlite"))
    monkeypatch.setattr(ledger, "_warned_at", None)
    caplog.set_level(logging.WARNING, logger="llm-proxy")
    await add()
    await add()
    warnings = [r for r in caplog.records if "Cost ledger write failed" in r.getMessage()]
    assert len(warnings) == 1, "one warning per window, not one per request"


def test_startup_logs_the_ledger_it_opened(caplog):
    caplog.set_level(logging.INFO, logger="llm-proxy")
    ledger.startup()
    assert any("Cost ledger at :memory:" in r.getMessage() for r in caplog.records)


async def test_an_explicit_window_picks_out_a_specific_week():
    """A calendar week or month, not just "the last N days": `since`/`until`
    are inclusive local dates and win over `days`."""
    for back, cost in ((12, 1.0), (9, 2.0), (6, 4.0), (3, 8.0), (0, 16.0)):
        await add(when=ago(back), usage=Usage(prompt=1, completion=1, cost=cost, currency="USD"))
    since, until = ledger.local_day(ago(9)), ledger.local_day(ago(3))
    s = await ledger.summary(days=1, since=since, until=until)
    assert s["range"] == {"days": 1, "since": since, "until": until, "day": None}
    assert s["totals"]["requests"] == 3 and s["totals"]["cost"] == pytest.approx(14.0)
    # The day series covers exactly the window, ends included, nothing outside it.
    assert [d["day"] for d in s["by_day"]] == [ledger.local_day(ago(b)) for b in (9, 6, 3)]
    assert s["models"] == ["glm"]


async def test_a_window_with_one_open_end():
    await add(when=ago(20))
    await add(when=ago(2))
    await add()
    s = await ledger.summary(since=ledger.local_day(ago(5)))
    assert s["totals"]["requests"] == 2 and s["range"]["until"] == ledger.local_day()
    s = await ledger.summary(until=ledger.local_day(ago(10)))
    assert s["totals"]["requests"] == 1 and s["range"]["since"] is None


async def test_a_window_in_the_past_has_no_today_in_it():
    await add(when=ago(40), usage=Usage(prompt=1, completion=1, cost=3.0, currency="USD"))
    await add()
    s = await ledger.summary(since=ledger.local_day(ago(45)), until=ledger.local_day(ago(35)))
    assert s["totals"]["requests"] == 1 and s["totals"]["cost"] == pytest.approx(3.0)


async def test_priced_tokens_exclude_requests_that_carried_no_price():
    """Cost per million tokens must divide by the tokens that were priced, or a
    backend looks cheaper the more unpriced traffic it also served."""
    await add(usage=Usage(prompt=900, completion=100, cost=0.5, currency="USD"))
    await add(usage=Usage(prompt=5000, completion=5000))
    t = (await ledger.summary())["totals"]
    assert t["priced_tokens"] == 1000 and t["prompt"] == 5900


async def test_ollama_hosts_are_one_backend_in_the_views_but_not_in_the_rows():
    await add(provider="ollamaCitrine", model="local.glm", asked="local.glm")
    await add(provider="ollamaGW", model="local.glm", asked="local.glm")
    await add(provider="ollamaLuis", model="local.glm", asked="local.glm")
    await add(provider="nano", usage=Usage(prompt=1, completion=1, cost=0.5, currency="USD"))

    s = await ledger.summary()
    by = {p["provider"]: p for p in s["by_provider"]}
    assert set(by) == {"ollama", "nano"} and by["ollama"]["requests"] == 3
    assert s["providers"] == ["nano", "ollama"]
    local = next(m for m in s["by_model"] if m["model"] == "local.glm")
    assert [(p["provider"], p["requests"]) for p in local["providers"]] == [("ollama", 3)]
    # The request list keeps the real host, so which box answered is not lost.
    assert {r["provider"] for r in s["requests"]} == {"ollamaCitrine", "ollamaGW", "ollamaLuis", "nano"}

    # Filtering on the group takes every host in it; a real name stays exact.
    assert (await ledger.summary(provider="ollama"))["totals"]["requests"] == 3
    assert (await ledger.summary(provider="ollamaGW"))["totals"]["requests"] == 1
    assert (await ledger.summary(provider="nano"))["totals"]["requests"] == 1


async def test_shares_describe_the_workload_whether_or_not_it_was_priced():
    """Cache share and output share are what the ledger adds to a published rate; they
    must exist for a backend whose responses carry no price at all."""
    await add(usage=Usage(prompt=900, completion=100, cached=720))
    await add(usage=Usage(prompt=100, completion=100, cached=0))
    got = await ledger.observed_shares(14)
    h, o, n = got[("glm", "nano")]
    assert (h, o, n) == (pytest.approx(720 / 1000), pytest.approx(200 / 1200), 2)


async def test_shares_ignore_failed_requests_and_empty_ones():
    await add(status=503, usage=Usage(prompt=100, completion=0, cached=100))
    await add(usage=Usage())
    assert await ledger.observed_shares(14) == {}


async def test_the_shares_window_is_bounded():
    await add(when=ago(20), usage=Usage(prompt=10, completion=10))
    assert await ledger.observed_shares(14) == {}
    assert len(await ledger.observed_shares(30)) == 1
