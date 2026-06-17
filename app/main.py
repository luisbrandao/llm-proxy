import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app import auth, persistence
from app import config as conf
from app.metrics import metrics_response
from app.proxy import proxy_request
from app.registry import list_models

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


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


def _unify_logging() -> None:
    """Align uvicorn's loggers with the rest of the app's format and stream.

    uvicorn installs its own handlers (the `INFO:     ...` style) on the
    uvicorn/uvicorn.access/uvicorn.error loggers with propagate=False, so they
    ignore basicConfig. Re-point them at our formatter — and at stdout — so
    every line matches and lives on one greppable stream.
    """
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(_formatter)
            # uvicorn's handlers are plain StreamHandlers on stderr; move them to
            # stdout. Guard against FileHandler (a StreamHandler subclass) so we
            # never redirect a file-backed handler.
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setStream(sys.stdout)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs after uvicorn has configured its logging, so our reformat sticks.
    _unify_logging()
    # Restore persisted counters, then snapshot them periodically and on shutdown.
    persistence.load()
    flush_task = asyncio.create_task(persistence.flush_loop())
    try:
        yield
    finally:
        flush_task.cancel()
        persistence.dump()


app = FastAPI(title="LLM Proxy", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


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
    logging.getLogger(__name__).info(
        "Logging flags updated: LOG_INPUT=%s LOG_OUTPUT=%s",
        conf.LOG_INPUT,
        conf.LOG_OUTPUT,
    )
    return {"log_input": conf.LOG_INPUT, "log_output": conf.LOG_OUTPUT}


@app.get("/models")
@app.get("/v1/models")
async def models(request: Request):
    return await list_models(authorized=auth.is_authorized(request))


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(request: Request, path: str):
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return await proxy_request(request, path)
