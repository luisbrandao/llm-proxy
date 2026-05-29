import json
import logging
import time
from datetime import timedelta
from typing import Union

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from app import config
from app.metrics import (
    ERRORS_TOTAL,
    REQUEST_DURATION,
    REQUESTS_TOTAL,
    TOKENS_INPUT_TOTAL,
    TOKENS_OUTPUT_TOTAL,
)

logger = logging.getLogger("deepseek-proxy")


def _build_url(path: str) -> str:
    return f"{config.DEEPSEEK_BASE_URL}/{path.lstrip('/')}"


def _build_headers(request: Request) -> dict:
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["authorization"] = f"Bearer {config.DEEPSEEK_API_KEY}"
    return headers


def _log_curl(method: str, url: str, headers: dict, body: str) -> None:
    cmd = f"{method} {url}"
    for k, v in headers.items():
        if k.lower() == "authorization":
            cmd += f"\n  -H '{k}: Bearer ***'"
        else:
            cmd += f"\n  -H '{k}: {v}'"
    if body:
        try:
            parsed = json.loads(body)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            cmd += f"\n  -d '{pretty}'"
        except (json.JSONDecodeError, ValueError):
            cmd += f"\n  -d '{body}'"
    logger.info(f"Request:\n{cmd}")


def _log_summary(model: str, in_tokens: int, out_tokens: int, duration: float) -> None:
    speed = out_tokens / duration if duration > 0 else 0
    time_str = str(timedelta(seconds=int(duration)))
    logger.info(
        f"Tokens - Model: {model}, In: {in_tokens}, Out: {out_tokens}, "
        f"Time: {time_str}, Speed: {speed:.2f} t/s"
    )


def _record_metrics(model: str, in_tokens: int, out_tokens: int, duration: float) -> None:
    REQUESTS_TOTAL.labels(model=model).inc()
    TOKENS_INPUT_TOTAL.labels(model=model).inc(in_tokens)
    TOKENS_OUTPUT_TOTAL.labels(model=model).inc(out_tokens)
    REQUEST_DURATION.labels(model=model).observe(duration)


async def _handle_non_stream(
    request: Request, path: str, body: bytes, body_str: str, model: str
) -> Response:
    url = _build_url(path)
    headers = _build_headers(request)
    method = request.method.upper()

    if config.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    start = time.time()

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.request(method, url, headers=headers, content=body)

        duration = time.time() - start
        resp_body = resp.text

        in_tokens = 0
        out_tokens = 0
        try:
            data = json.loads(resp_body)
            usage = data.get("usage", {})
            in_tokens = usage.get("prompt_tokens", 0)
            out_tokens = usage.get("completion_tokens", 0)
        except (json.JSONDecodeError, AttributeError):
            pass

        if config.LOG_OUTPUT:
            pretty_body = resp_body
            try:
                parsed = json.loads(resp_body)
                pretty_body = json.dumps(parsed, indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                pass
            logger.info(f"Response ({resp.status_code}):\n{pretty_body}")

        if resp.is_error:
            ERRORS_TOTAL.labels(model=model, status_code=str(resp.status_code)).inc()
        else:
            _record_metrics(model, in_tokens, out_tokens, duration)

        if in_tokens > 0 or out_tokens > 0:
            _log_summary(model, in_tokens, out_tokens, duration)

        resp_headers = dict(resp.headers)
        resp_headers.pop("content-length", None)
        resp_headers.pop("transfer-encoding", None)
        resp_headers.pop("content-encoding", None)

        return Response(
            content=resp_body,
            status_code=resp.status_code,
            headers=resp_headers,
        )


async def _handle_stream(
    request: Request, path: str, body: bytes, body_str: str, model: str
) -> StreamingResponse:
    url = _build_url(path)
    headers = _build_headers(request)
    method = request.method.upper()

    if config.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    async def generate():
        start = time.time()
        in_tokens = 0
        out_tokens = 0
        buffer = ""
        delta_contents = [] if config.LOG_OUTPUT else None
        last_usage = None
        resp_model = model
        status_code = 200
        error = False

        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                async with client.stream(method, url, headers=headers, content=body) as resp:
                    status_code = resp.status_code
                    if resp.is_error:
                        error = True
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                        text = chunk.decode("utf-8", errors="replace")
                        buffer += text
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                continue
                            try:
                                data = json.loads(payload)
                                resp_model = data.get("model", resp_model)
                                usage = data.get("usage")
                                if usage:
                                    in_tokens = usage.get("prompt_tokens", in_tokens)
                                    out_tokens = usage.get("completion_tokens", out_tokens)
                                    last_usage = usage
                                if delta_contents is not None:
                                    choices = data.get("choices", [])
                                    if choices:
                                        content = choices[0].get("delta", {}).get("content", "")
                                        if content:
                                            delta_contents.append(content)
                            except json.JSONDecodeError:
                                pass
        except Exception as e:
            error = True
            logger.error(f"Stream error: {e}")
        finally:
            duration = time.time() - start
            if error:
                ERRORS_TOTAL.labels(model=model, status_code=str(status_code)).inc()
            else:
                _record_metrics(model, in_tokens, out_tokens, duration)
            if in_tokens > 0 or out_tokens > 0:
                _log_summary(model, in_tokens, out_tokens, duration)
            if delta_contents is not None:
                full_text = "".join(delta_contents)
                meta = ""
                if last_usage:
                    meta += f"\n--- model: {resp_model}"
                    meta += f"\n--- prompt_tokens: {last_usage.get('prompt_tokens', 0)}"
                    meta += f"\n--- completion_tokens: {last_usage.get('completion_tokens', 0)}"
                    meta += f"\n--- total_tokens: {last_usage.get('total_tokens', 0)}"
                    pt_details = last_usage.get("prompt_tokens_details", {})
                    if pt_details:
                        meta += f"\n--- cached_tokens: {pt_details.get('cached_tokens', 0)}"
                    ct_details = last_usage.get("completion_tokens_details", {})
                    if ct_details:
                        meta += f"\n--- reasoning_tokens: {ct_details.get('reasoning_tokens', 0)}"
                logger.info(f"Stream response ({status_code}):{meta}\n{full_text}")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def proxy_request(request: Request, path: str) -> Union[Response, StreamingResponse]:
    body = await request.body()
    body_str = body.decode("utf-8", errors="replace")

    model = "unknown"
    is_stream = False
    try:
        payload = json.loads(body_str)
        model = payload.get("model", "unknown")
        is_stream = payload.get("stream", False)
    except (json.JSONDecodeError, AttributeError):
        pass

    if is_stream:
        return await _handle_stream(request, path, body, body_str, model)
    else:
        return await _handle_non_stream(request, path, body, body_str, model)
