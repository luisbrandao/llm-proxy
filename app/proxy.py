import gzip
import json
import logging
import time
import zlib
from datetime import timedelta
from typing import Optional, Tuple, Union

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from app import config as conf
from app.config import Provider
from app.metrics import (
    ERRORS_TOTAL,
    REQUEST_DURATION,
    REQUESTS_TOTAL,
    TOKENS_INPUT_TOTAL,
    TOKENS_OUTPUT_TOTAL,
)

logger = logging.getLogger("deepseek-proxy")


def resolve_provider(model: str) -> Tuple[Optional[Provider], str]:
    """Map an incoming model name to (provider, real_model).

    Routing order:
      0. Expand a global alias (simple name -> "provider:model").
      1. Explicit `provider:model` prefix.
      2. First provider whose enabled_models lists the bare name.
      3. Fallback: first configured provider.
    The provider's model_map is applied to the resolved name.
    """
    model = conf.ALIASES.get(model, model)
    sep = conf.PROVIDER_SEP
    if sep in model:
        prefix, _, rest = model.partition(sep)
        provider = conf.PROVIDERS_BY_NAME.get(prefix)
        if provider:
            return provider, provider.model_map.get(rest, rest)

    for provider in conf.PROVIDERS:
        if model in provider.enabled_models:
            return provider, provider.model_map.get(model, model)

    if conf.PROVIDERS:
        provider = conf.PROVIDERS[0]
        return provider, provider.model_map.get(model, model)

    return None, model


def _build_url(provider: Provider, path: str) -> str:
    return f"{provider.base_url}/{path.lstrip('/')}"


def _build_headers(provider: Provider, request: Request) -> dict:
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    if provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    else:
        headers.pop("authorization", None)
    # Only advertise encodings we can decode with the stdlib. Otherwise a
    # backend may reply with brotli, which we'd be unable to uncompress.
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


def _log_summary(provider: str, model: str, in_tokens: int, out_tokens: int, duration: float) -> None:
    speed = out_tokens / duration if duration > 0 else 0
    time_str = str(timedelta(seconds=int(duration)))
    logger.info(
        f"Tokens - Provider: {provider}, Model: {model}, In: {in_tokens}, Out: {out_tokens}, "
        f"Time: {time_str}, Speed: {speed:.2f} t/s"
    )


def _record_metrics(provider: str, model: str, in_tokens: int, out_tokens: int, duration: float) -> None:
    REQUESTS_TOTAL.labels(provider=provider, model=model).inc()
    TOKENS_INPUT_TOTAL.labels(provider=provider, model=model).inc(in_tokens)
    TOKENS_OUTPUT_TOTAL.labels(provider=provider, model=model).inc(out_tokens)
    REQUEST_DURATION.labels(provider=provider, model=model).observe(duration)


def _backend_error(provider: Provider, model: str, exc: Exception) -> Response:
    """Build a clean OpenAI-style error when an upstream backend is unreachable.

    Backends come and go, so a connection failure is an expected condition, not
    a crash: we translate it into a 502/504 the client can understand instead of
    letting it surface as an unhandled 500.
    """
    if isinstance(exc, httpx.TimeoutException):
        status, kind = 504, "upstream_timeout"
    else:
        status, kind = 502, "upstream_unavailable"

    logger.warning(f"Backend '{provider.name}' unreachable: {type(exc).__name__}: {exc}")
    if model != "unknown":
        ERRORS_TOTAL.labels(provider=provider.name, model=model, status_code=str(status)).inc()

    payload = {
        "error": {
            "message": f"Upstream backend '{provider.name}' is unavailable: {exc}",
            "type": kind,
            "code": status,
        }
    }
    return Response(
        content=json.dumps(payload),
        status_code=status,
        media_type="application/json",
    )


async def _handle_non_stream(
    request: Request, provider: Provider, path: str, body: bytes, body_str: str, model: str
) -> Response:
    url = _build_url(provider, path)
    headers = _build_headers(provider, request)
    method = request.method.upper()
    pname = provider.name

    if conf.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    start = time.time()

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream(method, url, headers=headers, content=body) as resp:
                # Read the raw, undecoded bytes so we control decompression
                # ourselves (httpx cannot decode brotli/zstd without extra libs
                # and would otherwise pass compressed bytes straight through).
                raw = b"".join([chunk async for chunk in resp.aiter_raw()])
                status_code = resp.status_code
                is_error = resp.is_error
                resp_headers = dict(resp.headers)
                content_encoding = resp.headers.get("content-encoding", "")
    except httpx.RequestError as e:
        return _backend_error(provider, model, e)

    duration = time.time() - start

    resp_bytes = _decompress(raw, content_encoding)
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
        logger.info(f"Response ({status_code}):\n{pretty_body}")

    if model != "unknown":
        if is_error:
            ERRORS_TOTAL.labels(provider=pname, model=model, status_code=str(status_code)).inc()
        else:
            _record_metrics(pname, model, in_tokens, out_tokens, duration)

    if in_tokens > 0 or out_tokens > 0:
        _log_summary(pname, model, in_tokens, out_tokens, duration)

    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)
    resp_headers.pop("content-encoding", None)

    return Response(
        content=resp_body,
        status_code=status_code,
        headers=resp_headers,
    )


async def _handle_stream(
    request: Request, provider: Provider, path: str, body: bytes, body_str: str, model: str
) -> Union[Response, StreamingResponse]:
    url = _build_url(provider, path)
    headers = _build_headers(provider, request)
    method = request.method.upper()
    pname = provider.name

    if conf.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    # Pre-flight the connection: a StreamingResponse commits its status code
    # before the body generator runs, so we must learn whether the backend is
    # reachable *now* — otherwise an offline backend would yield a 200 with an
    # empty body instead of a clean error.
    client = httpx.AsyncClient(timeout=600.0)
    stream_cm = client.stream(method, url, headers=headers, content=body)
    try:
        resp = await stream_cm.__aenter__()
    except httpx.RequestError as e:
        await client.aclose()
        return _backend_error(provider, model, e)

    async def generate():
        start = time.time()
        in_tokens = 0
        out_tokens = 0
        buffer = ""
        delta_contents = [] if conf.LOG_OUTPUT else None
        reasoning_contents = [] if conf.LOG_OUTPUT else None
        final_chunk = None
        resp_model = model
        status_code = resp.status_code
        error = resp.is_error

        try:
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
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()
            duration = time.time() - start
            if model != "unknown":
                if error:
                    ERRORS_TOTAL.labels(provider=pname, model=model, status_code=str(status_code)).inc()
                else:
                    _record_metrics(pname, model, in_tokens, out_tokens, duration)
            if in_tokens > 0 or out_tokens > 0:
                _log_summary(pname, model, in_tokens, out_tokens, duration)
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


def _routing_for(provider: Provider, model: str):
    """Resolve the upstream routing object for a model, or None.

    Looks up `provider.provider_routing` by resolved model id, falling back to
    a "*" default. A list value pins strictly (order + allow_fallbacks=false);
    a dict is passed through verbatim (full OpenRouter `provider` control).
    """
    routing = provider.provider_routing
    if not routing:
        return None
    spec = routing.get(model, routing.get("*"))
    if isinstance(spec, list):
        return {"order": spec, "allow_fallbacks": False}
    if isinstance(spec, dict):
        return spec
    return None


async def proxy_request(request: Request, path: str) -> Union[Response, StreamingResponse]:
    body = await request.body()
    body_str = body.decode("utf-8", errors="replace")

    provider: Optional[Provider] = None
    model = "unknown"
    is_stream = False

    try:
        payload = json.loads(body_str)
        raw_model = payload.get("model")
        is_stream = payload.get("stream", False)
        if raw_model:
            provider, model = resolve_provider(raw_model)
            if provider is not None:
                payload["model"] = model
                # Inject upstream routing (e.g. OpenRouter `provider`) unless the
                # client already specified its own — client wins.
                if "provider" not in payload:
                    routing = _routing_for(provider, model)
                    if routing is not None:
                        payload["provider"] = routing
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                body_str = body.decode("utf-8")
    except (json.JSONDecodeError, AttributeError):
        pass

    if provider is None:
        # No JSON body / no model field (e.g. non-chat calls): forward to first provider.
        if not conf.PROVIDERS:
            return Response(content="No providers configured", status_code=503)
        provider = conf.PROVIDERS[0]

    if is_stream:
        return await _handle_stream(request, provider, path, body, body_str, model)
    return await _handle_non_stream(request, provider, path, body, body_str, model)
