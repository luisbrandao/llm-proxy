import gzip
import json
import logging
import time
import zlib
from datetime import timedelta
from typing import Optional, Union

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from app import config as conf
from app import auth, registry, router, slots
from app.config import Provider
from app.metrics import (
    ERRORS_TOTAL,
    FAILOVERS_TOTAL,
    REQUEST_DURATION,
    REQUESTS_TOTAL,
    TOKENS_INPUT_TOTAL,
    TOKENS_OUTPUT_TOTAL,
)

logger = logging.getLogger("llm-proxy")


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

    # Connection failures propagate as httpx.RequestError so the dispatcher can
    # fail over to the next backend; the response is fully buffered here.
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
    request: Request, provider: Provider, path: str, body: bytes, body_str: str, model: str,
    on_complete=None,
) -> StreamingResponse:
    url = _build_url(provider, path)
    headers = _build_headers(provider, request)
    method = request.method.upper()
    pname = provider.name

    if conf.LOG_INPUT:
        _log_curl(method, url, headers, body_str)

    # Pre-flight the connection: a StreamingResponse commits its status code
    # before the body generator runs, so we must learn whether the backend is
    # reachable *now* — otherwise an offline backend would yield a 200 with an
    # empty body instead of a clean error. A failure here propagates as
    # httpx.RequestError so the dispatcher can fail over before any bytes are
    # committed to the client (failover is impossible mid-stream).
    client = httpx.AsyncClient(timeout=600.0)
    stream_cm = client.stream(method, url, headers=headers, content=body)
    try:
        resp = await stream_cm.__aenter__()
    except httpx.RequestError:
        await client.aclose()
        raise

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
            if on_complete is not None:
                await on_complete()
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


def _build_body(payload: dict, provider: Provider, model: str):
    """Rewrite the request body for a chosen target: set the real model id,
    inject upstream routing (OpenRouter `provider`), and ask for usage on streams.

    Each defers to the client: an explicit `provider` or `stream_options` wins.
    """
    p = dict(payload)
    p["model"] = model
    if "provider" not in p:
        routing = _routing_for(provider, model)
        if routing is not None:
            p["provider"] = routing
    # For streaming, ask the upstream to emit a final `usage` chunk so our token
    # metrics are reliable without depending on the client to request it.
    if p.get("stream"):
        opts = dict(p.get("stream_options") or {})
        opts.setdefault("include_usage", True)
        p["stream_options"] = opts
    body = json.dumps(p, ensure_ascii=False).encode("utf-8")
    return body, body.decode("utf-8")


async def _dispatch(
    request: Request, path: str, payload: dict, is_stream: bool, targets: list
) -> Union[Response, StreamingResponse]:
    """Acquire a slot on the best available target and forward, failing over to
    the next-priority target if a backend errors out (when failover is enabled)."""
    remaining = list(targets)
    last_provider = None
    last_exc = None

    while remaining:
        try:
            target = await slots.acquire(remaining, conf.ROUTING.queue_timeout)
        except slots.SlotTimeout:
            return Response(
                content=json.dumps({"error": {"message": "No backend slot available (queue timeout)", "type": "slot_timeout", "code": 503}}),
                status_code=503,
                media_type="application/json",
            )

        provider = conf.PROVIDERS_BY_NAME[target.provider]
        last_provider = provider
        body, body_str = _build_body(payload, provider, target.model)

        try:
            if is_stream:
                async def _release(p=target.provider):
                    await slots.release(p)

                resp = await _handle_stream(
                    request, provider, path, body, body_str, target.model, on_complete=_release
                )
                # Slot is released by the generator's finally once streaming ends.
                registry.clear_down(target.provider)
                return resp

            resp = await _handle_non_stream(request, provider, path, body, body_str, target.model)
            await slots.release(target.provider)
            registry.clear_down(target.provider)
            return resp
        except httpx.RequestError as e:
            await slots.release(target.provider)
            last_exc = e
            if not conf.ROUTING.failover:
                return _backend_error(provider, target.model, e)
            registry.mark_down(target.provider, conf.ROUTING.down_backoff)
            FAILOVERS_TOTAL.labels(provider=target.provider).inc()
            remaining = [t for t in remaining if t.provider != target.provider]
            logger.warning(
                f"Failover: '{target.provider}' failed ({type(e).__name__}); "
                f"{len(remaining)} target(s) left for model '{target.model}'"
            )

    # Every candidate failed.
    if last_exc is None:
        last_exc = httpx.ConnectError("no backends available")
    return _backend_error(last_provider, "unknown", last_exc)


def _unauthorized(model: str) -> Response:
    payload = {
        "error": {
            "message": f"Model '{model}' requires authentication" if model else "Authentication required",
            "type": "unauthorized",
            "code": 401,
        }
    }
    return Response(content=json.dumps(payload), status_code=401, media_type="application/json")


async def proxy_request(request: Request, path: str) -> Union[Response, StreamingResponse]:
    body = await request.body()
    body_str = body.decode("utf-8", errors="replace")

    authorized = auth.is_authorized(request)

    payload = None
    raw_model = None
    is_stream = False
    try:
        payload = json.loads(body_str)
        raw_model = payload.get("model")
        is_stream = payload.get("stream", False)
    except (json.JSONDecodeError, AttributeError):
        payload = None

    # No JSON model (e.g. non-chat passthrough): forward to the first provider
    # untouched, without slot gating.
    if not raw_model:
        if not conf.PROVIDERS:
            return Response(content="No providers configured", status_code=503)
        provider = conf.PROVIDERS[0]
        if not authorized and provider.require_permission:
            return _unauthorized("")
        try:
            if is_stream:
                return await _handle_stream(request, provider, path, body, body_str, "unknown")
            return await _handle_non_stream(request, provider, path, body, body_str, "unknown")
        except httpx.RequestError as e:
            return _backend_error(provider, "unknown", e)

    targets = await router.resolve(raw_model)
    if not targets:
        return Response(content="No providers configured", status_code=503)

    # Gate: unauthenticated callers can only reach open backends. If the model
    # lives solely behind permission-required backends, reject with 401.
    if not authorized:
        targets = [t for t in targets if not auth.restricted(t.provider)]
        if not targets:
            return _unauthorized(raw_model)

    # Prefer backends not currently marked down, but keep them as a last resort.
    healthy = [t for t in targets if not registry.is_down(t.provider)]
    candidates = healthy or targets

    return await _dispatch(request, path, payload, is_stream, candidates)
