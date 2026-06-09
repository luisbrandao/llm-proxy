import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app import auth, persistence
from app.metrics import metrics_response
from app.proxy import proxy_request
from app.registry import list_models

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT)


def _unify_logging() -> None:
    """Align uvicorn's loggers with the rest of the app's format.

    uvicorn installs its own handlers (the `INFO:     ...` style) on the
    uvicorn/uvicorn.access/uvicorn.error loggers with propagate=False, so they
    ignore basicConfig. Re-point them at our formatter so every line matches.
    """
    fmt = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(fmt)


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


@app.get("/models")
@app.get("/v1/models")
async def models(request: Request):
    return await list_models(authorized=auth.is_authorized(request))


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(request: Request, path: str):
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return await proxy_request(request, path)
