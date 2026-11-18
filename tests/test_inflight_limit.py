"""`GET /admin/inflight?limit=N`: the first N rows of the feed, for a small
client polling over a lossy link.

The console reads the route with no `limit`, so the one property that must hold
above all is that the response without it is unchanged. With it, the list is
cut, never reordered (live rows still first), and the counters keep counting
everything.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import inflight, registry
from test_admin_contract import AUTH, CONFIG


@pytest.fixture
def client(load_config, monkeypatch):
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    load_config(CONFIG + '\nauth:\n  keys: ["test-key"]\n')
    from app import main
    with TestClient(main.app) as c:
        yield c


def _begin(model):
    return inflight.begin(
        model=model, stream=False, op=None, method="POST", path="/v1/chat/completions",
        req_bytes=10, client_ip="127.0.0.1", svc=None,
    )


@pytest.fixture
def feed():
    """Five finished requests, then one running and two queued.
    Returns the ids in the order the feed lists them. Built inside a loop
    because an entry records the task that serves it."""
    return asyncio.run(_build_feed())


async def _build_feed():
    done = []
    for i in range(5):
        e = _begin(f"done-{i}")
        e.run("alpha", "native")
        e.record(200)
        e.finish()
        done.append(e.id)
    running = _begin("running")
    running.run("alpha", "native")
    queued = [_begin("queued-0"), _begin("queued-1")]
    live = [queued[1].id, queued[0].id, running.id]  # newest first
    return live + list(reversed(done))


def get(client, query=""):
    return client.get("/admin/inflight" + query, headers=AUTH)


def ids(body):
    return [r["id"] for r in body["requests"]]


def counters(body):
    return {k: body[k] for k in ("running", "queued", "history", "history_limit")}


TOTALS = {"running": 1, "queued": 2, "history": 5}


def test_without_limit_the_response_is_unchanged(client, feed):
    body = get(client).json()
    assert ids(body) == feed
    expected = inflight.snapshot()
    for key in ("running", "queued", "history", "history_limit", "requests"):
        if key == "requests":
            assert ids(body) == [r["id"] for r in expected["requests"]]
        else:
            assert body[key] == expected[key]
    assert {"providers", "queue_timeout", "queue_affinity", "affinity_max_skips"} <= body.keys()


@pytest.mark.parametrize("n", [1, 3, 4, 6])
def test_limit_keeps_the_first_rows_live_ones_first(client, feed, n):
    body = get(client, f"?limit={n}").json()
    assert ids(body) == feed[:n]
    assert counters(body) == {**TOTALS, "history_limit": inflight._recent.maxlen}
    assert {"providers", "queue_timeout", "queue_affinity", "affinity_max_skips"} <= body.keys()


def test_limit_zero_returns_no_rows_and_the_real_counters(client, feed):
    body = get(client, "?limit=0").json()
    assert body["requests"] == []
    assert counters(body) == {**TOTALS, "history_limit": inflight._recent.maxlen}


def test_a_limit_past_the_end_returns_everything(client, feed):
    assert ids(get(client, "?limit=1000").json()) == feed


@pytest.mark.parametrize("value", ["-1", "abc", "1.5"])
def test_an_invalid_limit_is_422(client, feed, value):
    assert get(client, f"?limit={value}").status_code == 422
