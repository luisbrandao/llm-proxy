"""The fields the web console reads from each admin view.

`app/static/app.js` is vanilla JS with no build step and no type checking, so a
renamed or dropped field in an admin payload surfaces as a silently blank cell in
the UI rather than as any kind of error. These tests pin the contract.

The field lists below were taken from the console itself — every `p.<field>` and
`t.<field>` it dereferences. Adding a field is fine; removing or renaming one
must break a test here, not a tab.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import ledger, registry
from app.usage import Usage

CONFIG = """\
models:
  grouped:
    targets:
      - {provider: alpha, priority: 1}
      - {provider: beta,  model: "other/pinned-beta", priority: 2}

providers:
  - name: alpha
    base_url: "http://127.0.0.1:1/v1"
    slots: 2
    cache_ttl: 30
    strip_path_prefix: "v1"
    api_key: "sk-not-for-export"
    enabled_models: ["vendor/Alpha-Grouped"]
    model_map:
      "vendor/Alpha-Grouped": grouped

  - name: beta
    base_url: "http://127.0.0.1:2/v1"
    require_permission: true

aliases:
  quick: "alpha:grouped"

routing:
  queue_timeout: 0
  failover: true
  auto_group: true
  down_backoff: 15
  queue_affinity: true
  affinity_max_skips: 3
"""

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def client(load_config, monkeypatch):
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    load_config(CONFIG + '\nauth:\n  keys: ["test-key"]\n')
    from app import main
    with TestClient(main.app) as c:
        yield c


# Every p.<field> the console dereferences, per view.
INFLIGHT_PROVIDER_FIELDS = {"name", "slots", "in_use", "is_down", "resident"}
ROUTING_PROVIDER_FIELDS = {
    "name", "kind", "base_url", "slots", "in_use", "is_down", "require_permission",
    "lists_all", "priority",
}
CONFIG_PROVIDER_FIELDS = {
    "name", "kind", "base_url", "slots", "priority", "require_permission", "cache_ttl",
    "strip_path_prefix", "lists_all", "enabled_models", "model_map", "fronted",
    "has_api_key",
}


def test_inflight_provider_fields(client):
    body = client.get("/admin/inflight", headers=AUTH).json()
    for p in body["providers"]:
        assert INFLIGHT_PROVIDER_FIELDS <= set(p), f"missing: {INFLIGHT_PROVIDER_FIELDS - set(p)}"


def test_inflight_snapshot_fields(client):
    body = client.get("/admin/inflight", headers=AUTH).json()
    assert {"running", "queued", "history", "history_limit", "requests",
            "providers", "queue_timeout", "queue_affinity",
            "affinity_max_skips"} <= set(body)


def test_routing_provider_fields(client):
    body = client.get("/admin/routing", headers=AUTH).json()
    assert {"auto_group", "config_writable", "providers", "logical_models", "aliases"} <= set(body)
    for p in body["providers"]:
        assert ROUTING_PROVIDER_FIELDS <= set(p), f"missing: {ROUTING_PROVIDER_FIELDS - set(p)}"


def test_config_provider_fields(client):
    body = client.get("/admin/config", headers=AUTH).json()
    assert {"path", "writable", "providers", "logical_models", "aliases", "routing"} <= set(body)
    for p in body["providers"]:
        assert CONFIG_PROVIDER_FIELDS <= set(p), f"missing: {CONFIG_PROVIDER_FIELDS - set(p)}"


def test_no_view_leaks_an_api_key(client):
    for path in ("/admin/config", "/admin/routing", "/admin/inflight"):
        text = client.get(path, headers=AUTH).text
        assert "sk-not-for-export" not in text, f"{path} leaked the api_key"
        assert '"api_key"' not in text, f"{path} exposes an api_key field"


def test_routing_targets_report_a_resolved_model(client):
    """The routing view matches a reorder against the resolved native id, so it
    must report `model` already resolved."""
    body = client.get("/admin/routing", headers=AUTH).json()
    grouped = next(m for m in body["logical_models"] if m["name"] == "grouped")
    by_provider = {t["provider"]: t for t in grouped["targets"]}
    # alpha inherits its id from model_map — the routing view resolves it.
    assert by_provider["alpha"]["model"] == "vendor/Alpha-Grouped"
    assert by_provider["beta"]["model"] == "other/pinned-beta"
    for t in grouped["targets"]:
        assert {"provider", "model", "priority", "is_down", "known_provider"} <= set(t)


def test_config_targets_keep_an_inherited_model_null(client):
    """The editor must see `model: null` for an inherited target, or saving the
    form unchanged writes an explicit pin and changes what the config means."""
    body = client.get("/admin/config", headers=AUTH).json()
    grouped = next(m for m in body["logical_models"] if m["name"] == "grouped")
    by_provider = {t["provider"]: t for t in grouped["targets"]}

    assert by_provider["alpha"]["model"] is None, "an inherited target must stay null"
    assert by_provider["alpha"]["resolved_model"] == "vendor/Alpha-Grouped"
    # A pinned target reports the pin in both places.
    assert by_provider["beta"]["model"] == "other/pinned-beta"
    assert by_provider["beta"]["resolved_model"] == "other/pinned-beta"


def test_config_reports_which_ids_a_group_fronts(client):
    body = client.get("/admin/config", headers=AUTH).json()
    alpha = next(p for p in body["providers"] if p["name"] == "alpha")
    assert alpha["fronted"] == {"vendor/Alpha-Grouped": "grouped"}


def test_config_routing_block_fields(client):
    body = client.get("/admin/config", headers=AUTH).json()
    assert {"queue_timeout", "failover", "auto_group", "down_backoff",
            "queue_affinity", "affinity_max_skips"} <= set(body["routing"])


def test_a_target_on_a_removed_provider_stays_visible_and_flagged(client, load_config):
    """`known_provider: false` is what keeps a half-finished config edit fixable
    in the console instead of just disappearing."""
    from app import config as conf
    conf.LOGICAL_MODELS["orphan"] = conf.LogicalModel(
        name="orphan", targets=[conf.Target(provider="deleted", priority=1)]
    )
    body = client.get("/admin/routing", headers=AUTH).json()
    orphan = next(m for m in body["logical_models"] if m["name"] == "orphan")
    assert orphan["targets"][0]["known_provider"] is False


# Every field the Costs tab reads from /admin/costs: the envelope, the shared
# aggregate shape (totals, by_day, by_model, by_provider and each model's
# provider split all carry it), and the request rows.
COSTS_FIELDS = {
    "enabled", "path", "retention_days", "rows", "oldest", "today", "range", "filters",
    "totals", "by_day", "by_model", "by_provider", "requests", "limit", "models",
    "providers", "currencies",
}
COSTS_AGG_FIELDS = {
    "requests", "errors", "priced", "cost", "prompt", "completion", "cached", "cache_write",
    "reasoning", "priced_tokens",
}
COSTS_ROW_FIELDS = {
    "at", "day", "provider", "model", "asked", "served", "status", "stream", "op", "duration",
    "prompt", "completion", "cached", "cache_write", "reasoning", "cost", "currency", "svc",
    "client",
}


def test_costs_fields(client):
    asyncio.run(ledger.record(
        provider="alpha", model="vendor/Alpha-Grouped", asked="grouped", served=None,
        status=200, stream=True, op=None, duration=2.0,
        usage=Usage(prompt=10, completion=5, cached=4, cost=0.01, currency="USD"),
        svc="svc", client="host",
    ))
    body = client.get("/admin/costs?days=7", headers=AUTH).json()
    assert COSTS_FIELDS <= set(body), f"missing: {COSTS_FIELDS - set(body)}"
    assert {"days", "since", "until", "day"} <= set(body["range"]) and {"model", "provider"} <= set(body["filters"])
    assert COSTS_AGG_FIELDS <= set(body["totals"])
    for group in ("by_day", "by_model", "by_provider"):
        assert body[group], f"{group} is empty"
        for row in body[group]:
            assert COSTS_AGG_FIELDS <= set(row), f"{group} missing: {COSTS_AGG_FIELDS - set(row)}"
    assert "day" in body["by_day"][0] and "provider" in body["by_provider"][0]
    model = body["by_model"][0]
    assert "model" in model and model["providers"]
    assert {"provider", "model"} | COSTS_AGG_FIELDS <= set(model["providers"][0])
    for row in body["requests"]:
        assert COSTS_ROW_FIELDS <= set(row), f"missing: {COSTS_ROW_FIELDS - set(row)}"


def test_costs_rejects_a_malformed_day(client):
    r = client.get("/admin/costs?day=yesterday", headers=AUTH)
    assert r.status_code == 422 and "day" in r.json()["error"]


# Every field the Routing tab reads from `smart` in /admin/routing.
SMART_FIELDS = {"enabled", "window_days", "min_requests", "refreshed_at", "providers", "prices", "models"}


def test_routing_view_carries_the_smart_block(client):
    from app import smart
    smart._prices = {("grouped", "alpha"): (1.5, 12)}
    body = client.get("/admin/routing", headers=AUTH).json()
    s = body["smart"]
    assert SMART_FIELDS <= set(s), f"missing: {SMART_FIELDS - set(s)}"
    assert {"name", "kind", "mode", "locked"} <= set(s["providers"][0])
    assert {"model", "provider", "usd_per_mtok", "requests"} <= set(s["prices"][0])
    assert {"name", "static", "current", "by"} <= set(s["models"][0])
    assert set(client.get("/admin/config", headers=AUTH).json()["routing"]) >= {"smart_routing", "backend_modes"}


def test_the_smart_switch_and_modes_are_written_and_applied(client):
    from app import config as conf
    r = client.put("/admin/config/smart", headers=AUTH, json={"enabled": True, "modes": {"beta": "last"}})
    assert r.status_code == 200 and r.json()["persisted"] is True
    assert conf.ROUTING.smart_routing is True and conf.ROUTING.backend_modes == {"beta": "last"}
    assert client.get("/admin/routing", headers=AUTH).json()["smart"]["enabled"] is True
    # A later call may carry only one of the two; the other is left alone.
    client.put("/admin/config/smart", headers=AUTH, json={"enabled": False})
    assert conf.ROUTING.smart_routing is False and conf.ROUTING.backend_modes == {"beta": "last"}
    client.put("/admin/config/smart", headers=AUTH, json={"modes": {"beta": "auto"}})
    assert conf.ROUTING.backend_modes == {}


@pytest.mark.parametrize("body", [
    {}, {"enabled": "yes"}, {"modes": ["alpha"]}, {"modes": {"nope": "first"}}, {"modes": {"alpha": "frist"}},
])
def test_the_smart_endpoint_refuses_bad_input(client, body):
    r = client.put("/admin/config/smart", headers=AUTH, json=body)
    assert r.status_code == 422 and "error" in r.json()


# Every field the Costs tab reads from /admin/accounts.
ACCOUNT_FIELDS = {"name", "vendor", "balance", "quota", "state", "low_threshold", "updated_at", "rates_at", "error"}
QUOTA_FIELDS = {"used", "limit", "remaining", "percent", "reset_at", "allow_overage", "available", "mode"}


def test_accounts_fields(client, monkeypatch):
    from app import config as conf
    from app import rates
    from app.providers import Balance, Quota
    monkeypatch.setattr(conf.PROVIDERS_BY_NAME["alpha"], "vendor", "nanogpt")
    rates._quotas["alpha"] = Quota(used=58, limit=60, remaining=2, reset_at=1792972800.0,
                                   allow_overage=True, available=True, mode="subscription")
    rates._balances["alpha"] = Balance("USD", 15.99)
    body = client.get("/admin/accounts", headers=AUTH).json()
    assert {"accounts", "rates_at", "cards"} <= set(body)
    row = body["accounts"][0]
    assert ACCOUNT_FIELDS <= set(row), f"missing: {ACCOUNT_FIELDS - set(row)}"
    assert QUOTA_FIELDS <= set(row["quota"]) and {"currency", "amount"} <= set(row["balance"])
    assert "sk-not-for-export" not in str(body)


def test_costs_never_leaks_an_api_key(client):
    text = client.get("/admin/costs", headers=AUTH).text
    assert "sk-not-for-export" not in text and '"api_key"' not in text


def test_costs_accepts_an_explicit_window_and_rejects_a_bad_one(client):
    ok = client.get("/admin/costs?since=2026-09-01&until=2026-09-30", headers=AUTH)
    assert ok.status_code == 200
    assert ok.json()["range"]["since"] == "2026-09-01" and ok.json()["range"]["until"] == "2026-09-30"
    for query in ("since=nope", "until=2026-13-01", "since=2026-10-02&until=2026-10-01"):
        r = client.get("/admin/costs?" + query, headers=AUTH)
        assert r.status_code == 422, query
