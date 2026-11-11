"""The admin API: everything under `/admin`, the backend of the web console.

Gated by the same bearer keys as the restricted backends and `POST /logging`.
The gate is required, not cosmetic: the log buffer can hold full request and
response bodies once LOG_INPUT/LOG_OUTPUT are on. It is structural — every route
here is declared on `router`, which carries the dependency — so a new endpoint
cannot forget it.

Provider serialization deliberately omits `api_key`: `_provider_view`,
`_serialize_targets` and `_config_snapshot` are the only serializers the views
use, and none of them emits a secret.
"""
import asyncio
import logging
import time
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app import auth, configwrite, hotreload, inflight, ledger, logbuffer, rates, registry, slots, smart
from app import config as conf

logger = logging.getLogger("llm-proxy")


class Forbidden(HTTPException):
    """403 for the admin gate. `main` registers a handler that renders it as
    `{"error": "unauthorized"}`, the body the console and anything scripting
    /admin/* read, where a plain HTTPException would render `{"detail": ...}`.
    """

    def __init__(self):
        super().__init__(status_code=403, detail="unauthorized")


def require_admin(request: Request) -> None:
    """Dependency enforcing the admin bearer gate. Raises 403 when it fails.

    A dependency rather than a helper each handler remembers to call: the gate is
    load-bearing (the log buffer can hold full prompt and response bodies once
    LOG_INPUT/LOG_OUTPUT are on), and an endpoint that forgot the call would be
    unprotected with nothing to catch it. Attached to `router` below, so every
    route on it inherits the gate and the invariant is structural.
    """
    if not auth.is_authorized(request):
        raise Forbidden()


# Every /admin/* route lives on this router, so the gate above applies to all of
# them by construction. `main` includes it above the catch-all proxy route.
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _levelno(name: str) -> int:
    value = logging.getLevelName(name)
    return value if isinstance(value, int) else logging.INFO


def _provider_view(p, *, live=False, editable=False, fronted=None) -> dict:
    """One provider, serialized for an admin view. **Never** includes `api_key`.

    The one serializer behind every view that shows a provider, so a new
    `Provider` field is added in one place. The views nest, hence two switches:

    * `live` adds the runtime state the in-flight and routing views need
      (occupancy, health, resident model) — read from `slots`/`registry`, not the
      config.
    * `editable` adds the settings the Config tab edits, including `has_api_key`
      in place of the key itself, so the secret stays in-process.
    """
    out = {
        "name": p.name,
        "kind": p.kind,
        "base_url": p.base_url,
        "slots": p.slots,
        "priority": p.priority,
        "require_permission": p.require_permission,
        "lists_all": p.lists_all,
    }
    if live:
        out.update({
            "in_use": slots.in_use(p.name),
            "is_down": registry.is_down(p.name),
            # What this backend most recently ran, i.e. what it is expected to
            # still have loaded — the basis of the queue's affinity decision.
            "resident": slots.resident_model(p.name),
        })
    if editable:
        out.update({
            "cache_ttl": p.cache_ttl,
            "strip_path_prefix": p.strip_path_prefix,
            "enabled_models": list(p.enabled_models),
            "model_map": dict(p.model_map),
            "fronted": (fronted or {}).get(p.name, {}),
            "has_api_key": bool(p.api_key),
        })
    return out


def _serialize_targets(model_name: str, lm, raw: bool = False) -> list:
    """A logical model's targets, for either the routing view or the editor.

    The two differ in exactly one thing, and it matters: `raw=False` reports
    `model` already resolved, which is the stable identity the routing view
    matches a reorder against. `raw=True` reports `model` **as written** (null
    when inherited) plus a separate `resolved_model`, because an editor that got
    the resolved id back would write it into the file on the next save — silently
    turning every inherited target into an explicit pin and changing what the
    config means without changing what it says.
    """
    out = []
    for t in lm.targets:
        resolved = conf.native_for(model_name, t.provider, t.model)
        row = {
            "provider": t.provider,
            "priority": t.priority,
            "is_down": registry.is_down(t.provider),
            "known_provider": t.provider in conf.PROVIDERS_BY_NAME,
        }
        if raw:
            row["model"] = t.model
            row["resolved_model"] = resolved
        else:
            row["model"] = resolved
        out.append(row)
    return out


@router.get("/logs")
async def admin_logs(since: int = 0, level: str = "DEBUG"):
    """Recent log lines with seq > `since` (the UI's live-tail cursor).

    `last_seq` always reflects the newest buffered line — even one filtered out
    by `level` — so the cursor advances past filtered lines instead of re-pulling
    them on every poll.
    """
    new = logbuffer.handler.entries(since)
    last_seq = new[-1]["seq"] if new else since
    threshold = _levelno((level or "DEBUG").upper())
    if threshold > logging.DEBUG:
        new = [e for e in new if _levelno(e["level"]) >= threshold]
    return {"entries": new, "last_seq": last_seq}


@router.get("/upstream-models")
async def admin_upstream_models(provider: str = ""):
    """Probe each backend's real /v1/models concurrently — the 'bypass' button.

    Shows every id a backend actually serves, regardless of its enabled_models
    allow-list, so each endpoint's full catalog is visible in one place.
    """

    async def probe(p):
        try:
            ids = await registry._fetch_live(p)
            return {"provider": p.name, "ok": True, "ids": sorted(ids)}
        except Exception as e:  # noqa: BLE001 - report per-backend, never 500 the page
            return {"provider": p.name, "ok": False, "error": f"{type(e).__name__}: {e}"}

    # `?provider=name` probes just one backend: the Config tab needs a single
    # catalog when a provider is expanded, and probing all eight (cloud APIs
    # included) to fill one checkbox list would take seconds.
    wanted = [p for p in conf.PROVIDERS if not provider or p.name == provider]
    if provider and not wanted:
        return _not_found(f"unknown provider '{provider}'")
    results = await asyncio.gather(*(probe(p) for p in wanted))
    return {"providers": results}


@router.get("/inflight")
async def admin_inflight():
    """Everything currently in flight: requests holding a slot and requests
    queued behind one, arrival-ordered, plus each provider's slot occupancy.

    Read-only snapshot of process-local state (see app/inflight.py) — cheap
    enough for the console to poll every second. The provider block is the same
    slot accounting the Routing tab shows, repeated here so the queue and the
    capacity that gates it read side by side.
    """
    snap = inflight.snapshot()
    snap["providers"] = [_provider_view(p, live=True) for p in conf.PROVIDERS]
    snap["queue_timeout"] = conf.ROUTING.queue_timeout
    snap["queue_affinity"] = conf.ROUTING.queue_affinity
    snap["affinity_max_skips"] = conf.ROUTING.affinity_max_skips
    return snap


@router.post("/inflight/{request_id}/cancel")
async def admin_cancel_inflight(request_id: int):
    """Kill one in-flight request — the console's per-row Kill button.

    Cancels the asyncio task serving it (see `inflight.Entry.cancel`), which
    unwinds the normal cleanup: slot released, upstream connection closed, entry
    deregistered. Works on a queued request and on one blocked in an upstream
    read that may never return.

    404 means the id is unknown, which almost always means the request finished on
    its own between the console's last poll and the click — not an error worth
    alarming anyone about.

    This is logged here, at WARNING, because it is a deliberate operator action
    *and* because the killed request may lose its own `event=request` line: the
    cancellation can interrupt the background task that emits it.
    """
    entry = inflight.get(request_id)
    if entry is None:
        return _not_found(f"no in-flight request with id {request_id} — it already finished")
    logger.warning(
        "Cancelling in-flight request #%d: %s (%s) on %s, %s for %.1fs — operator action",
        entry.id,
        entry.model or entry.path,
        entry.state,
        entry.provider or "no backend yet",
        "streaming" if entry.stream else "buffered",
        max(0.0, time.monotonic() - entry.arrived),
    )
    entry.cancel()
    return {"id": entry.id, "status": "cancelled", "state": entry.state}


@router.get("/inflight/{request_id}/body")
async def admin_inflight_body(request_id: int):
    """The prompt and reply captured for one row of the feed.

    Deliberately not part of the snapshot: an agentic client's prompt runs to
    hundreds of KiB, and the console polls every second. Fetched per row instead,
    only when someone opens it.

    404 covers three cases that are all "nothing to show": capture disabled
    (`INFLIGHT_BODIES=false`), the row aged out of the history, or the request
    carried no body at all.
    """
    if not conf.INFLIGHT_BODIES:
        return _not_found("body capture is disabled (INFLIGHT_BODIES=false)")
    rec = inflight.bodies(request_id)
    if rec is None:
        return _not_found(f"no captured body for request {request_id}")
    return {"id": request_id, "limit": conf.INFLIGHT_BODY_LIMIT, **rec}




@router.get("/accounts")
async def admin_accounts():
    """Each backend's balance and subscription quota, as the vendors last reported them.

    Read from the snapshot `app/rates.py` keeps fresh in the background (quota and
    balances every couple of minutes), so this never calls a vendor itself. Numbers
    only: a key is never part of it. A backend that could not be read carries its error
    and the age of the last good reading, rather than disappearing.
    """
    return rates.accounts_snapshot()


@router.get("/costs")
async def admin_costs(
    days: int = 30, day: str = "", model: str = "", provider: str = "",
    limit: int = 200, since: str = "", until: str = "",
):
    """What each model cost, per day and per request — the Costs tab.

    Read from the cost ledger (app/ledger.py), the one per-request record that
    survives a restart. The window is `days` (default 30; `0` = everything the
    ledger keeps) or, to look at a specific week or month, `since` / `until`
    (YYYY-MM-DD, inclusive; either may be given alone, and they win over `days`).
    `day` narrows the totals, the model table and the request list to one local
    date while the per-day series keeps the whole window so the chart stays
    navigable, and `model` (the name the client sent) and `provider` filter
    everything. `limit` caps the request list (newest first).

    A ledger that cannot be read is a 503 with the reason, never a traceback:
    the proxy itself is fine, only the tab is dark.
    """
    parsed = {}
    for name, value in (("day", day), ("since", since), ("until", until)):
        if value:
            try:
                parsed[name] = date.fromisoformat(value)
            except ValueError:
                return _bad(f"{name} must be YYYY-MM-DD")
    if "since" in parsed and "until" in parsed and parsed["since"] > parsed["until"]:
        return _bad("since must not be after until")
    try:
        return await ledger.summary(
            days=max(0, days), day=day or None, model=model or None,
            provider=provider or None, limit=max(1, min(limit, 2000)),
            since=since or None, until=until or None,
        )
    except Exception as e:  # noqa: BLE001 - a broken ledger file darkens one tab, not the proxy
        logger.warning("Cost ledger read failed: %s: %s", type(e).__name__, e)
        return JSONResponse(
            {"error": f"cost ledger unavailable: {type(e).__name__}: {e}"}, status_code=503
        )


def _fronted_natives() -> dict:
    """`{provider: {native_id: group_name}}` — which backend ids a logical model
    already fronts.

    The editor needs it to answer the only question that matters about a pinned
    id: *what does a client actually have to send for this?* A group fronting an
    id means clients use the group's name and the raw id is hidden from
    `/v1/models` (see `registry.list_models`), so showing the id alone is
    misleading. Computed here rather than in the browser because
    `conf.native_for` already knows how an inherited target resolves.
    """
    out = {}
    for name, lm in conf.LOGICAL_MODELS.items():
        for t in lm.targets:
            out.setdefault(t.provider, {}).setdefault(
                conf.native_for(name, t.provider, t.model), name
            )
    return out


def _config_snapshot() -> dict:
    """The config as the console edits it. **Never** includes `api_key`: a
    provider reports only whether one is set (`has_api_key`), preserving the rule
    that secrets do not leave the process."""
    fronted = _fronted_natives()
    return {
        "path": conf.CONFIG_PATH,
        "writable": configwrite.config_writable(),
        # True when the host file was replaced under a running container, so
        # anything saved here would vanish on the next restart. See
        # configwrite.config_detached — it is silent without this flag.
        "detached": configwrite.config_detached(),
        "providers": [
            _provider_view(p, editable=True, fronted=fronted) for p in conf.PROVIDERS
        ],
        "logical_models": [
            {"name": name, "targets": _serialize_targets(name, lm, raw=True)}
            for name, lm in conf.LOGICAL_MODELS.items()
        ],
        "aliases": dict(conf.ALIASES),
        "routing": {
            "queue_timeout": conf.ROUTING.queue_timeout,
            "failover": conf.ROUTING.failover,
            "auto_group": conf.ROUTING.auto_group,
            "down_backoff": conf.ROUTING.down_backoff,
            "queue_affinity": conf.ROUTING.queue_affinity,
            "affinity_max_skips": conf.ROUTING.affinity_max_skips,
            "smart_routing": conf.ROUTING.smart_routing,
            "backend_modes": dict(conf.ROUTING.backend_modes),
        },
    }


async def _persisted(ok: bool, error, what: str):
    """Shared tail of every config mutation: reload on success, then hand back the
    fresh snapshot so the console re-renders from the file rather than from what it
    hoped it wrote."""
    if ok:
        logger.info("Config edit from the console: %s", what)
        await hotreload.after_console_edit()
    else:
        logger.warning("Config edit refused (%s): %s", what, error)
    body = _config_snapshot()
    body["persisted"] = ok
    body["error"] = error
    return JSONResponse(body, status_code=200 if ok else 422)


def _bad(message: str):
    return JSONResponse({"error": message}, status_code=422)


def _not_found(message: str):
    return JSONResponse({"error": message}, status_code=404)


@router.get("/config")
async def admin_config():
    """The editable config: providers (no secrets), logical models, aliases,
    routing. Backs the console's Config tab."""
    return _config_snapshot()


@router.put("/config/providers/{name}/enabled-models")
async def admin_set_enabled_models(name: str, request: Request):
    """Set which upstream models a backend may serve.

    An empty list is meaningful, not a no-op: it means "expose everything this
    backend live-reports" (`Provider.lists_all`), which is how the ollama boxes are
    configured. So the editor can move a provider in both directions.
    """
    if name not in conf.PROVIDERS_BY_NAME:
        return _not_found(f"unknown provider '{name}'")
    body = await request.json()
    models = body.get("models")
    if models is None:
        models = []
    if not isinstance(models, list) or any(
        not isinstance(m, str) or not m.strip() for m in models
    ):
        return _bad('body must be {"models": ["native-id", ...]} of non-empty strings')
    cleaned = list(dict.fromkeys(m.strip() for m in models))
    ok, error = await configwrite.set_enabled_models(name, cleaned)
    return await _persisted(ok, error, f"{name}.enabled_models = {len(cleaned)} model(s)")


@router.put("/config/providers/{name}/model-map")
async def admin_set_model_map(name: str, request: Request):
    """Rename a backend's native ids for clients: `{"model_map": {native: canonical}}`.

    The mapping must be a **bijection** — two native ids cannot share one canonical
    name on the same provider. `Provider.__post_init__` builds the reverse lookup by
    inverting this dict, so a collision silently makes one of them unreachable by
    canonical name (verified: the last one wins). That is refused here rather than
    written, with the advice that the multi-backend case wants a group instead.
    """
    if name not in conf.PROVIDERS_BY_NAME:
        return _not_found(f"unknown provider '{name}'")
    body = await request.json()
    mapping = body.get("model_map")
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        return _bad('body must be {"model_map": {"native-id": "canonical-name", ...}}')
    cleaned = {}
    for native, canonical in mapping.items():
        native, canonical = str(native).strip(), str(canonical).strip()
        if not native or not canonical:
            return _bad("every mapping needs both a native id and a name")
        if any(c.isspace() for c in native) or any(c.isspace() for c in canonical):
            return _bad(
                f"'{native or canonical}' contains a space — a model id is a single "
                f"token; enter the native id and the name in separate fields"
            )
        if native in cleaned:
            return _bad(f"native id '{native}' is listed twice")
        cleaned[native] = canonical
    dupes = {c for c in cleaned.values() if list(cleaned.values()).count(c) > 1}
    if dupes:
        return _bad(
            f"two native ids both map to {sorted(dupes)} — model_map must be one-to-one, "
            f"or the reverse lookup silently picks one. To serve one name from several "
            f"backends, make a group instead."
        )
    ok, error = await configwrite.set_model_map(name, cleaned)
    return await _persisted(ok, error, f"{name}.model_map = {len(cleaned)} mapping(s)")


@router.put("/config/aliases")
async def admin_set_aliases(request: Request):
    """Replace the alias map. An alias shadows everything else in resolution, so
    one pointing at an unknown provider would silently break a model name — those
    are refused rather than written."""
    body = await request.json()
    aliases = body.get("aliases")
    if not isinstance(aliases, dict):
        return _bad('body must be {"aliases": {"name": "target", ...}}')
    cleaned = {}
    for key, value in aliases.items():
        key, value = str(key).strip(), str(value).strip()
        if not key or not value:
            return _bad("alias names and targets must both be non-empty")
        if conf.PROVIDER_SEP in value:
            prefix = value.split(conf.PROVIDER_SEP, 1)[0]
            if prefix not in conf.PROVIDERS_BY_NAME:
                return _bad(
                    f"alias '{key}' points at unknown provider '{prefix}' — "
                    f"use 'provider:model' with a configured provider, or a bare model name"
                )
        cleaned[key] = value
    ok, error = await configwrite.set_aliases(cleaned)
    return await _persisted(ok, error, f"aliases = {sorted(cleaned)}")


@router.put("/config/models/{name}")
async def admin_set_logical_model(name: str, request: Request):
    """Create or replace a logical model (a "group"): one client-facing name in
    front of an ordered list of backend targets."""
    name = name.strip()
    if not name:
        return _bad("model name must not be empty")
    if name in conf.ALIASES:
        return _bad(
            f"'{name}' is already an alias — an alias is resolved first, so the "
            f"logical model would never be reached. Remove the alias first."
        )
    body = await request.json()
    targets = body.get("targets")
    if not isinstance(targets, list) or not targets:
        return _bad('body must be {"targets": [{"provider", "priority", "model"?}, ...]}')
    cleaned = []
    for item in targets:
        if not isinstance(item, dict):
            return _bad("each target must be an object")
        provider = str(item.get("provider", "")).strip()
        if provider not in conf.PROVIDERS_BY_NAME:
            return _bad(f"unknown provider '{provider}'")
        try:
            priority = int(item.get("priority", 100))
        except (TypeError, ValueError):
            return _bad(f"target {provider}: priority must be an integer")
        if priority < 1:
            return _bad(f"target {provider}: priority must be 1 or greater")
        native = item.get("model")
        native = str(native).strip() if native else None
        cleaned.append({"provider": provider, "priority": priority, "model": native})
    seen = {(t["provider"], t["model"]) for t in cleaned}
    if len(seen) != len(cleaned):
        return _bad("duplicate (provider, model) target in the list")
    cleaned.sort(key=lambda t: t["priority"])
    ok, error = await configwrite.set_logical_model(name, cleaned)
    return await _persisted(ok, error, f"models['{name}'] = {len(cleaned)} target(s)")


@router.delete("/config/models/{name}")
async def admin_delete_logical_model(name: str):
    """Drop a logical model. Its clients fall back to the ordinary resolution
    order, which for same-named backends is auto-group."""
    if name not in conf.LOGICAL_MODELS:
        return _not_found(f"unknown logical model '{name}'")
    ok, error = await configwrite.delete_logical_model(name)
    return await _persisted(ok, error, f"deleted models['{name}']")


@router.get("/routing")
async def admin_routing():
    """The routing graph: providers (with live slot/health state), explicit
    logical models and their prioritized targets, and aliases. No api_key."""
    providers = [_provider_view(p, live=True) for p in conf.PROVIDERS]
    logical_models = [
        {"name": name, "editable": True, "targets": _serialize_targets(name, lm)}
        for name, lm in conf.LOGICAL_MODELS.items()
    ]
    return {
        "auto_group": conf.ROUTING.auto_group,
        "config_writable": configwrite.config_writable(),
        "config_detached": configwrite.config_detached(),
        "providers": providers,
        "logical_models": logical_models,
        "aliases": conf.ALIASES,
        "smart": smart.snapshot(),
    }


@router.put("/config/smart")
async def admin_set_smart(request: Request):
    """The smart-routing switch and the per-backend overrides.

    `{"enabled": bool}` and/or `{"modes": {provider: "auto"|"fixed"|"first"|"last"}}`;
    either may be given alone. `modes` replaces the whole mapping. Applied to the
    live process and written to the config file, like every other console edit.
    """
    body = await request.json()
    enabled, modes = body.get("enabled"), body.get("modes")
    if enabled is None and modes is None:
        return _bad('body must carry "enabled" and/or "modes"')
    if enabled is not None and not isinstance(enabled, bool):
        return _bad("enabled must be true or false")
    if modes is not None:
        if not isinstance(modes, dict):
            return _bad('modes must be {"provider": "auto|fixed|first|last", ...}')
        cleaned = {}
        for name, mode in modes.items():
            name, mode = str(name), str(mode).strip().lower()
            if name not in conf.PROVIDERS_BY_NAME:
                return _bad(f"unknown provider '{name}'")
            if mode not in conf.BACKEND_MODES:
                return _bad(f"'{mode}' is not one of {', '.join(conf.BACKEND_MODES)}")
            cleaned[name] = mode
        modes = cleaned
    ok, error = await configwrite.set_smart(enabled, modes)
    what = ", ".join(
        part for part in (
            f"smart_routing = {enabled}" if enabled is not None else "",
            f"backend_modes = {sorted(m for m, v in modes.items() if v != 'auto')}" if modes is not None else "",
        ) if part
    )
    return await _persisted(ok, error, what)


@router.post("/routing/{model}")
async def admin_set_routing(model: str, request: Request):
    """Rearrange a logical model's target priorities: applied live, then persisted
    into the config file (surgical priority-only rewrite; see app/configwrite.py).

    Reorder only — the (provider, model) set must match exactly. The new priorities
    are written to the live Targets and the list re-sorted so the slot picker's
    priority tiers stay correct on the next request. Persistence is best-effort:
    on a read-only mount (or unrecognized config format) the live change stands
    and the response says `persisted: false` with the reason.
    """
    lm = conf.LOGICAL_MODELS.get(model)
    if lm is None:
        return _not_found(f"unknown logical model '{model}'")

    body = await request.json()
    incoming = body.get("targets")
    if not isinstance(incoming, list) or not incoming:
        return _bad('body must be {"targets": [{"provider","model","priority"}, ...]}')
    try:
        wanted = {(item["provider"], item["model"]): int(item["priority"]) for item in incoming}
    except (KeyError, TypeError, ValueError):
        return _bad("each target needs provider, model and an integer priority")

    existing = {
        (t.provider, conf.native_for(model, t.provider, t.model)): t for t in lm.targets
    }
    if set(wanted) != set(existing):
        return _bad(
            "targets must match the model's existing (provider, model) set exactly — "
            "reorder only, no add/remove"
        )

    for key, target in existing.items():
        target.priority = wanted[key]
    lm.targets.sort(key=lambda t: t.priority)
    logger.info(
        "Routing priorities updated for '%s': %s",
        model,
        ", ".join(f"{t.provider}={t.priority}" for t in lm.targets),
    )

    persisted, persist_error = await configwrite.persist_model_priorities(model, lm.targets)
    if not persisted:
        logger.warning(
            "Routing change for '%s' applied live but NOT persisted: %s", model, persist_error
        )
    return {
        "name": model,
        "editable": True,
        "targets": _serialize_targets(model, lm),
        "persisted": persisted,
        "persist_error": persist_error,
    }
