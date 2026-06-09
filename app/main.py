import logging

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.metrics import metrics_response
from app.proxy import proxy_request
from app.registry import list_models

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(title="LLM Proxy")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    data, status, headers = metrics_response()
    return Response(content=data, status_code=status, headers=headers)


@app.get("/models")
@app.get("/v1/models")
async def models():
    return await list_models()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(request: Request, path: str):
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return await proxy_request(request, path)
