"""ASGI entry: logging setup, the lifespan, the public routes, the console's
static files, and the catch-all that hands everything else to the proxy.

The admin API lives in app/admin.py; this module only mounts it.
"""
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app import admin, auth, hotreload, ledger, logbuffer, notify, rates, smart, upstream, version
from app import config as conf
from app.metrics import metrics_response
from app.proxy import proxy_request
from app.registry import list_models

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

# The one logger name the whole process logs under, so the console's logger
# column reads the same for every line.
logger = logging.getLogger("llm-proxy")


class _LocalTimeFormatter(logging.Formatter):
    """Stamp every line with a local-time ISO-8601 timestamp that carries an
    explicit UTC offset, e.g. `2026-06-17T02:48:13-03:00`.

    Two problems this solves: the timestamp is unambiguous no matter what
    timezone the reader (Loki, a teammate) assumes, and "local" follows the
    container's TZ — so set `TZ` (e.g. America/Sao_Paulo) and ship tzdata in
    the image and the wall-clock matches where the box actually is.
    """

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


# All logs go to stdout (Python's StreamHandler — and uvicorn — default to
# stderr, which means `docker logs <c> | grep` silently misses everything until
# you add `2>&1`). One stream, greppable by default.
_formatter = _LocalTimeFormatter(LOG_FORMAT)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_handler])

# Per-request events are emitted as pure logfmt (`ts=.. level=.. event=request ..`)
# on a dedicated logger with a message-only formatter — no human prefix to trip
# Loki's `| logfmt`. Kept off the root handler via propagate=False so it isn't
# double-stamped. Everything else keeps the readable `<ts> LEVEL <msg>` format.
_event_handler = logging.StreamHandler(sys.stdout)
_event_handler.setFormatter(logging.Formatter("%(message)s"))
_event_logger = logging.getLogger("llm-proxy.event")
_event_logger.setLevel(logging.INFO)
_event_logger.addHandler(_event_handler)
_event_logger.propagate = False


# Mirror every log line into an in-memory ring buffer so the /admin/logs view can
# tail logs without a file or docker-socket access. Attached to the root logger
# (catches `llm-proxy` and anything else that propagates) and, separately, to the
# event logger (propagate=False, so its lines never reach root). uvicorn's own
# loggers are wired up in _unify_logging, after uvicorn installs its handlers.
def _attach_buffer(target: logging.Logger) -> None:
    if logbuffer.handler not in target.handlers:
        target.addHandler(logbuffer.handler)


_attach_buffer(logging.getLogger())
_attach_buffer(_event_logger)


# Access-log paths that are pure noise: the web console tails logs by polling
# /admin/logs every ~1.5s and the in-flight view by polling /admin/inflight every
# ~1s, which would otherwise flood the very view you're watching (and docker
# logs). Matched as a substring of the rendered access line.
_ACCESS_LOG_MUTE = ("/admin/logs", "/admin/inflight")


class _MutePollingFilter(logging.Filter):
    """Drop uvicorn access-log records for the console's own polling endpoints."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let a filter crash logging
            return True
        return not any(p in msg for p in _ACCESS_LOG_MUTE)


def _unify_logging() -> None:
    """Align uvicorn's loggers with the rest of the app's format and stream.

    uvicorn installs its own handlers (the `INFO:     ...` style) on the
    uvicorn/uvicorn.access/uvicorn.error loggers with propagate=False, so they
    ignore basicConfig. Re-point them at our formatter — and at stdout — so
    every line matches and lives on one greppable stream.
    """
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        for handler in uvicorn_logger.handlers:
            handler.setFormatter(_formatter)
            # uvicorn's handlers are plain StreamHandlers on stderr; move them to
            # stdout. Guard against FileHandler (a StreamHandler subclass) so we
            # never redirect a file-backed handler.
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setStream(sys.stdout)
        # uvicorn's loggers have propagate=False, so the buffer on root won't see
        # them — attach it directly so access/error lines show up in /admin/logs.
        _attach_buffer(uvicorn_logger)

    # Mute the console's own log-tail polling at the access logger, so those lines
    # reach neither stdout nor the ring buffer (otherwise the Logging tab floods
    # itself every poll).
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _MutePollingFilter) for f in access.filters):
        access.addFilter(_MutePollingFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs after uvicorn has configured its logging, so our reformat sticks.
    _unify_logging()
    # First line that matters in `docker logs`: what is actually running.
    logger.info(
        "llm-proxy %s starting — %d providers, %d logical models, %d aliases",
        version.summary(), len(conf.PROVIDERS), len(conf.LOGICAL_MODELS), len(conf.ALIASES),
    )
    # Open the cost ledger now, so a bad path is one WARNING at boot rather than
    # a surprise on the first request, and the retention prune runs before traffic.
    ledger.startup()
    reload_task = (
        asyncio.create_task(hotreload.watch())
        if conf.CONFIG_RELOAD_INTERVAL > 0
        else None
    )
    # Observed prices for smart routing, re-read from the ledger off the request path.
    smart_task = asyncio.create_task(smart.refresh_loop())
    # Published rates, subscription quota and balances, from the vendors' own APIs.
    rates_task = asyncio.create_task(rates.refresh_loop())
    # Warnings (low balance, used-up allowance) to ntfy, when `notify:` is configured.
    notify_task = asyncio.create_task(notify.loop())
    try:
        yield
    finally:
        if reload_task is not None:
            reload_task.cancel()
        smart_task.cancel()
        rates_task.cancel()
        notify_task.cancel()
        # Close the shared upstream connection pools (see app/upstream.py).
        await upstream.aclose()
        ledger.close()


app = FastAPI(title="LLM Proxy", lifespan=lifespan)


@app.get("/health")
async def health():
    """Liveness, plus which build is answering.

    The version is here rather than behind the admin gate because this is the
    endpoint a deploy already curls to confirm the container came up, and the
    question it could not answer was "is this actually the build I just pinned?"
    A compose file pinning `master-52` while a stale container keeps serving
    `master-50` is invisible from the outside otherwise.

    Unauthenticated, like `/metrics` — this is a build counter, not a
    CVE-mappable version string, on a proxy that already lists its model catalog
    to anonymous callers.
    """
    return {"status": "ok", **version.as_dict()}


@app.get("/metrics")
async def metrics():
    data, status, headers = metrics_response()
    return Response(content=data, status_code=status, headers=headers)


@app.get("/logging")
async def get_logging():
    return {"log_input": conf.LOG_INPUT, "log_output": conf.LOG_OUTPUT}


@app.post("/logging")
async def set_logging(request: Request):
    """Toggle request/response logging at runtime, no restart needed.

    Body: {"log_input": bool, "log_output": bool} — both keys optional.
    Gated by the same bearer keys as restricted backends (inert when unset).
    """
    if not auth.is_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    body = await request.json()
    for key, attr in (("log_input", "LOG_INPUT"), ("log_output", "LOG_OUTPUT")):
        if key in body:
            value = body[key]
            if not isinstance(value, bool):
                return JSONResponse(
                    {"error": f"{key} must be a boolean"}, status_code=422
                )
            setattr(conf, attr, value)
    logger.info(
        "Logging flags updated: LOG_INPUT=%s LOG_OUTPUT=%s",
        conf.LOG_INPUT,
        conf.LOG_OUTPUT,
    )
    return {"log_input": conf.LOG_INPUT, "log_output": conf.LOG_OUTPUT}


@app.get("/models")
@app.get("/v1/models")
async def models(request: Request):
    return await list_models(authorized=auth.is_authorized(request))


@app.exception_handler(admin.Forbidden)
async def _admin_forbidden_handler(request: Request, exc: admin.Forbidden):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# The admin API (app/admin.py). Every /admin/* route is declared on its router,
# which carries the auth dependency. Included above the catch-all proxy route
# so /admin/* wins over the proxy path.
app.include_router(admin.router)


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/favicon.ico")
async def favicon():
    """Serve a real icon instead of letting the browser's automatic request fall
    through to the catch-all, where it was resolved as a *model* name: with no
    JSON body it took the passthrough path, got auth-gated into a 401, and showed
    up in the request feed as a failed request. Every browser asks for this on
    every page load, so it was pure noise in the one view meant to be readable."""
    return FileResponse(
        os.path.join(_STATIC_DIR, "favicon.ico"),
        media_type="image/vnd.microsoft.icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/robots.txt")
async def robots():
    """Same reasoning as the favicon: a crawler or browser probing this path is
    not a model request. Nothing here should be indexed."""
    return Response(content="User-agent: *\nDisallow: /\n", media_type="text/plain")


# Convenience redirects to the dashboard. The StaticFiles mount only serves
# `/ui/...`; a bare `/ui` — or someone guessing `/admin` — would otherwise fall
# through to the catch-all proxy and get a confusing 401. Send them to /ui/.
@app.get("/ui")
@app.get("/admin")
@app.get("/admin/")
async def _ui_redirect():
    return RedirectResponse(url="/ui/")


# Static web dashboard. Mounted before the catch-all so /ui/* wins over the proxy;
# html=True serves index.html at /ui/. Lives under app/static (already COPYd in).
app.mount(
    "/ui",
    StaticFiles(directory=_STATIC_DIR, html=True),
    name="ui",
)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(request: Request, path: str):
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return await proxy_request(request, path)
