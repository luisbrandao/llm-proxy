import gzip
import json
import logging
import time
import zlib
from datetime import timedelta
from typing import Union

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from app import config as conf
from app.metrics import (
    ERRORS_TOTAL,
    REQUEST_DURATION,
    REQUESTS_TOTAL,
    TOKENS_INPUT_TOTAL,
    TOKENS_OUTPUT_TOTAL,
)

logger = logging.getLogger("deepseek-proxy")


def _build_url(path: str) -> str:
    return f"{conf.DEEPSEEK_BASE_URL}/{path.lstrip('/')}"


def _build_headers(request: Request) -> dict:
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["authorization"] = f"Bearer {conf.DEEPSEEK_API_KEY}"
    # Only advertise encodings we can decode with the stdlib. Otherwise
    # DeepSeek may reply with brotli, which we'd be unable to uncompress.
    headers["accept-encoding"] = "gzip, deflate"
    return headers


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Decompress an upstream response body based on its Content-Encoding.

    Handles gzip, deflate, brotli and zstd. Unknown or empty encodings are
    returned unchanged. If decompression fails the raw bytes are returned so
    the proxy never crashes on an unexpected body.
    """
    encoding = (encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return raw

    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                # Raw deflate stream without zlib header/trailer.
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if encoding == "br":
            import brotli  # type: ignore

            return brotli.decompress(raw)
        if encoding == "zstd":
            import zstandard  # type: ignore

            return zstandard.ZstdDecompressor().decompress(raw)
    except Exception as e:  # noqa: BLE001 - never let decompression crash the proxy
        logger.warning(f"Failed to decompress '{encoding}' response: {e}")
        return raw

    logger.warning(f"Unknown Content-Encoding '{encoding}', passing body through")
    return raw


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

    if conf.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    start = time.time()

    async with httpx.AsyncClient(timeout=600.0) as client:
        async with client.stream(method, url, headers=headers, content=body) as resp:
            # Read the raw, undecoded bytes so we control decompression
            # ourselves (httpx cannot decode brotli/zstd without extra libs and
            # would otherwise pass compressed bytes straight through).
            raw = b"".join([chunk async for chunk in resp.aiter_raw()])

        duration = time.time() - start

        resp_bytes = _decompress(raw, resp.headers.get("content-encoding", ""))
        resp_body = resp_bytes.decode("utf-8", errors="replace")

        in_tokens = 0
        out_tokens = 0
        try:
            data = json.loads(resp_body)
            usage = data.get("usage", {})
            in_tokens = usage.get("prompt_tokens", 0)
            out_tokens = usage.get("completion_tokens", 0)
        except (json.JSONDecodeError, AttributeError):
            pass

        if conf.LOG_OUTPUT:
            pretty_body = resp_body
            try:
                parsed = json.loads(resp_body)
                pretty_body = json.dumps(parsed, indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                pass
            logger.info(f"Response ({resp.status_code}):\n{pretty_body}")

        if model != "unknown":
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

    if conf.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    async def generate():
        start = time.time()
        in_tokens = 0
        out_tokens = 0
        buffer = ""
        delta_contents = [] if conf.LOG_OUTPUT else None
        reasoning_contents = [] if conf.LOG_OUTPUT else None
        final_chunk = None
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
                                    final_chunk = data
                                if delta_contents is not None:
                                    choices = data.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        content = delta.get("content", "")
                                        reasoning = delta.get("reasoning_content")
                                        if content:
                                            delta_contents.append(content)
                                        if reasoning:
                                            reasoning_contents.append(reasoning)
                            except json.JSONDecodeError:
                                pass
        except Exception as e:
            error = True
            logger.error(f"Stream error: {e}")
        finally:
            duration = time.time() - start
            if model != "unknown":
                if error:
                    ERRORS_TOTAL.labels(model=model, status_code=str(status_code)).inc()
                else:
                    _record_metrics(model, in_tokens, out_tokens, duration)
            if in_tokens > 0 or out_tokens > 0:
                _log_summary(model, in_tokens, out_tokens, duration)
            if delta_contents is not None:
                full_text = "".join(delta_contents)
                reasoning_text = "".join(reasoning_contents) if reasoning_contents else ""
                log_data = {}
                if final_chunk:
                    log_data = final_chunk
                else:
                    log_data = {
                        "model": resp_model,
                        "usage": {"prompt_tokens": in_tokens, "completion_tokens": out_tokens},
                    }
                log_data["_assembled_content"] = full_text
                if reasoning_text:
                    log_data["_reasoning_content"] = reasoning_text
                logger.info(
                    f"Stream response ({status_code}):\n{json.dumps(log_data, indent=2, ensure_ascii=False)}"
                )

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
        model = conf.MODEL_MAP.get(model, model)
        is_stream = payload.get("stream", False)
    except (json.JSONDecodeError, AttributeError):
        pass

    if is_stream:
        return await _handle_stream(request, path, body, body_str, model)
    else:
        return await _handle_non_stream(request, path, body, body_str, model)
