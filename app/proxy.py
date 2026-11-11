import asyncio
import gzip
import json
import logging
import math
import time
import zlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from app import config as conf
from app import auth, claude_cli, clientinfo, inflight, ledger, notify, rates, registry, router, slots, smart, trim, upstream
from app.config import Provider
from app.metrics import (
    COST_TOTAL,
    ERRORS_TOTAL,
    FAILOVERS_TOTAL,
    REQUEST_DURATION,
    REQUESTS_TOTAL,
    TOKENS_CACHED_TOTAL,
    TOKENS_INPUT_TOTAL,
    TOKENS_OUTPUT_TOTAL,
)
from app.providers import get_vendor
from app.usage import Usage, format_cost
from app.usage import parse as parse_usage

logger = logging.getLogger("llm-proxy")
# Pure-logfmt, prefix-free per-request events (configured in app.main).
event_logger = logging.getLogger("llm-proxy.event")


def _build_url(provider: Provider, path: str) -> str:
    return f"{provider.base_url}/{conf.strip_prefix(provider, path)}"


def _client_for(provider: Provider) -> httpx.AsyncClient:
    """Where a provider's requests go out: the shared HTTP pool, or — for the
    `claude-cli` kind — the transport that runs the CLI (app/claude_cli.py).
    Both are process-wide; never aclose() either from a request path."""
    if provider.kind == conf.KIND_CLAUDE_CLI:
        return claude_cli.client()
    return upstream.forward_client()


def _build_headers(provider: Provider, request: Request) -> dict:
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    if provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    else:
        headers.pop("authorization", None)
    # Operator-configured per-backend headers (e.g. OpenRouter attribution:
    # HTTP-Referer / X-Title). Applied as defaults — a header the client already
    # sent wins, so per-app attribution still passes through. Keys are lowercased
    # to match the forwarded set (HTTP header names are case-insensitive) and
    # avoid sending a duplicate.
    for k, v in provider.headers.items():
        headers.setdefault(k.lower(), v)
    # Forward the caller's User-Agent verbatim. When the caller sent none, httpx
    # would otherwise inject its own `python-httpx/x.y` default, which surfaces at
    # the backend (e.g. OpenRouter) as a bogus/"missing" client. Ensuring the key
    # is always present stops that substitution; absent a caller UA we advertise
    # the proxy itself rather than httpx.
    headers.setdefault("user-agent", "llm-proxy")
    # Only advertise encodings we can decode with the stdlib. Otherwise a
    # backend may reply with brotli, which we'd be unable to uncompress.
    headers["accept-encoding"] = "gzip, deflate"
    return headers


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Decompress an upstream response body based on its Content-Encoding.

    Handles exactly what `_build_headers` advertises — gzip and deflate, both
    stdlib. Keep the two aligned: asking for an encoding we cannot decode means a
    backend may answer with a body we can only pass through compressed. To
    support another one, add the library to requirements.txt, add the branch
    here, *and* advertise it in `_build_headers`.

    Unknown or empty encodings are returned unchanged, and a decompression failure
    returns the raw bytes, so this never crashes the proxy on an odd body.
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
    except Exception as e:  # noqa: BLE001 - never let decompression crash the proxy
        logger.warning(f"Failed to decompress '{encoding}' response: {e}")
        return raw

    logger.warning(f"Unknown Content-Encoding '{encoding}', passing body through")
    return raw


def _pretty_json(text: str) -> str:
    """`text` re-indented for the log when it is JSON, unchanged when it is not."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


def _log_curl(method: str, url: str, headers: dict, body: str) -> None:
    cmd = f"{method} {url}"
    for k, v in headers.items():
        if k.lower() == "authorization":
            cmd += f"\n  -H '{k}: Bearer ***'"
        else:
            cmd += f"\n  -H '{k}: {v}'"
    if body:
        cmd += f"\n  -d '{_pretty_json(body)}'"
    logger.info(f"Request:\n{cmd}")


def _logfmt(fields: dict) -> str:
    """Render an ordered dict as a logfmt line (key=value, space-separated).

    Values are quoted when they would otherwise break tokenization (contain
    whitespace, quotes or '='). None values are dropped so absent fields just
    don't appear. Parses cleanly in Loki/Grafana with `| logfmt`.
    """
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        s = str(value)
        if s == "" or any(c in s for c in ' "=\n\r\t'):
            s = '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ").replace("\t", " ") + '"'
        parts.append(f"{key}={s}")
    return " ".join(parts)


def _err_kind(status: int) -> Optional[str]:
    """A short, stable error category for an HTTP status (None when not an error)."""
    if status < 400:
        return None
    if status in (401, 403):
        return "unauthorized"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status < 500:
        return "invalid_request"
    return "upstream_error"


def _op_kind(path: str, model: str):
    """Classify no-output operations so the request log can tag them and skip the
    meaningless output-tokens/sec. Embeddings and rerankers return no completion
    tokens, so `out` is always 0 and a tokens/s figure would be a constant 0.00.

    Matched against both the request path (`/v1/embeddings`, `/v1/rerank`, …) and
    the model id (e.g. a `*-Reranker-*` / `*-Embedding-*` name), so it still works
    when a backend serves these over a nonstandard path. Returns None for ordinary
    generation. Extend the keyword list for other no-output ops (e.g. moderation).
    """
    hay = f"{path} {model}".lower()
    if "embed" in hay:
        return "embedding"
    if "rerank" in hay:
        return "rerank"
    return None


async def _emit_request_log(
    request: Request, provider: str, model: Optional[str], status: int,
    usage: Usage, duration: float, stream: bool,
    entry: Optional[inflight.Entry] = None, served: Optional[str] = None,
) -> None:
    """Emit the single, always-on, parseable line summarizing one request, and
    append the same record to the cost ledger.

    Carries who called (ip/host/service), what ran (provider/model), the
    outcome (status, token counts, speed, price) and whether it streamed. Runs
    after the response is delivered (background task / stream finally) so the
    reverse-DNS lookup never adds latency to the client.

    `usage` is everything the backend's usage block said (app/usage.py). Past
    `in=`/`out=`, the fields are emitted only when the backend reported them:
    `cached=` / `cache_write=` (prompt-cache reads and writes), `reasoning=`
    (thinking tokens), and `cost=` — the price in `usage.cost`, which OpenRouter
    and NanoGPT send and most others do not. `cost=0` is a real statement (a free
    model) and is kept; an absent field means the backend did not say. The ledger
    row (app/ledger.py) holds the same numbers, so the Costs tab and a Loki query
    over `cost=` agree.

    `model` is the **native** id that went on the wire, and stays that way for
    compatibility with existing dashboards and recording rules. `asked` is the
    name the *client* sent (the in-flight `entry` carries it, along with what the
    guardrail and the router did), emitted as its own `asked=` field, because those two
    routinely differ and only one of them is a stable thing to group by: a
    logical model spanning three backends resolves to three different native ids
    via each provider's model_map, so `model=` alone splits one model's traffic
    across three series — and a request that fails over moves between them
    mid-flight. `asked=` is what somebody actually typed into a client, so it is
    the field to sum over. Both are kept: the native id is what you need to ask a
    backend about its own logs.

    `served` is the third name: the `model` the backend's *response* reports
    having run, emitted as `served=` when it differs from the native id. A
    native id is often a rolling alias — the claude CLI's `opus`, an
    aggregator's unversioned slug — and this is the only place the concrete
    version that answered (`claude-opus-5-5`) is recorded. Omitted when the
    backend echoes the id it was sent, so most lines are unchanged.

    A model-less passthrough (`model` is None: a non-chat or multipart body,
    nothing to resolve) is logged as `event=passthrough` keyed by request path,
    so it never pollutes the per-model view.

    What the context guardrail did to this request (app/trim.py) is emitted as
    `trimmed=<messages dropped>` and `trim_capped=<tool results cut>` so a
    trimmed request is greppable in Loki; absent when nothing was trimmed.
    """
    asked = entry.model if entry is not None else None
    trimmed = entry.trimmed if entry is not None else None
    route = entry.routed["by"] if entry is not None and entry.routed else None
    ip = clientinfo.client_ip(request)
    host = await clientinfo.client_host(ip)
    ua = clientinfo.user_agent(request)
    fields = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "level": "info",
    }
    if model is None:
        fields.update({
            "event": "passthrough",
            "provider": provider,
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "stream": "true" if stream else "false",
        })
    else:
        # Embeddings and rerankers return no completion tokens, so out-tokens/sec
        # is always 0.00 and skews throughput dashboards. Tag the op and report
        # input throughput (in_tps) instead of speed_tps. The None-valued fields
        # are dropped by _logfmt, so ordinary generation lines are unchanged.
        op = _op_kind(request.url.path, model)
        fields.update({
            "event": "request",
            "provider": provider,
            "model": model,
            # The canonical name the client sent, when it differs from the native
            # id above. Dropped by _logfmt when equal or absent, so an unmapped
            # model logs exactly as it did before.
            "asked": asked if asked and asked != model else None,
            # What the backend says actually ran, when that is not the id we sent.
            "served": served if served and served != model else None,
            "op": op,
            "status": status,
            "stream": "true" if stream else "false",
            "in": usage.prompt,
            "out": usage.completion,
            # Reported-only fields: dropped by _logfmt when the backend said nothing.
            "cached": usage.cached or None,
            "cache_write": usage.cache_write or None,
            "reasoning": usage.reasoning or None,
            "cost": format_cost(usage.cost),
            # Present only when the price is the proxy's list-price estimate
            # (the provider's `pricing:` table), not what the backend charged.
            "cost_src": "table" if usage.estimated else None,
            # H:MM:SS, rounded UP to the whole second so a sub-second request
            # reads 0:00:01, never a misleading 0:00:00.
            "dur": str(timedelta(seconds=math.ceil(duration))),
            "speed_tps": None if op else f"{usage.completion / duration if duration > 0 else 0:.2f}",
            "in_tps": f"{usage.prompt / duration if duration > 0 else 0:.2f}" if op else None,
            "trimmed": trimmed["dropped"] if trimmed else None,
            "trim_capped": trimmed["capped"] if trimmed else None,
            # Present only when smart routing or a first/last override reordered
            # the backends for this request: `smart`, `pinned` or `smart+pinned`.
            "route": route,
        })
    svc = clientinfo.service_from_ua(ua)
    fields.update({
        "client_ip": ip,
        "client_host": host,
        "svc": svc,
        "ua": ua,
        "err": _err_kind(status),
    })
    event_logger.info(_logfmt(fields))
    if model is not None:
        # The durable copy the console can read back (Loki holds the line above).
        await ledger.record(
            provider=provider, model=model, asked=asked, served=served, status=status,
            stream=stream, op=op, duration=duration, usage=usage, svc=svc,
            client=host or ip,
        )


def _log_upstream_error(provider: str, model: Optional[str], status: int, body: str) -> None:
    """Log an upstream error response with its body, always.

    LOG_OUTPUT only gates successful-response logging; a backend rejecting a
    request (bad param, auth, overload) must be diagnosable from the proxy log
    alone — the body is where Google/OpenRouter/ollama say *why*.
    """
    snippet = _pretty_json(body.strip())
    if len(snippet) > 4000:
        snippet = snippet[:4000] + "... [truncated]"
    qualifier = "passthrough" if model is None else f"model: {model}"
    logger.warning(f"Upstream error {status} from '{provider}' ({qualifier}):\n{snippet}")


def _apply_pricing(provider: Provider, model: Optional[str], usage: Usage, started: float) -> None:
    """Price a request from the provider's `pricing:` table when the backend did not.

    A price the backend reported always wins, and a request with no token counts
    (no usage block at all) stays unpriced — zero tokens is "did not say", not
    "free". Priced at the request's start, so a request straddling the edge of a
    peak window is billed the way it was admitted.
    """
    if usage.cost is not None or provider.pricing is None:
        return
    if not (usage.prompt or usage.completion):
        return
    cost = provider.pricing.price(
        model, usage.prompt, usage.cached, usage.completion,
        datetime.fromtimestamp(started, timezone.utc),
    )
    if cost is not None:
        usage.cost, usage.currency, usage.estimated = cost, "USD", True


def _record_metrics(provider: str, model: str, usage: Usage, duration: float) -> None:
    REQUESTS_TOTAL.labels(provider=provider, model=model).inc()
    TOKENS_INPUT_TOTAL.labels(provider=provider, model=model).inc(usage.prompt)
    TOKENS_OUTPUT_TOTAL.labels(provider=provider, model=model).inc(usage.completion)
    if usage.cached:
        TOKENS_CACHED_TOTAL.labels(provider=provider, model=model).inc(usage.cached)
    # USD only: the counter has one unit, and every backend that prices a request
    # today reports dollars. Another currency still reaches the ledger, labelled.
    if usage.cost is not None and usage.cost >= 0 and (usage.currency or "USD") == "USD":
        COST_TOTAL.labels(provider=provider, model=model).inc(usage.cost)
    REQUEST_DURATION.labels(provider=provider, model=model).observe(duration)


def _error(status: int, message: str, kind: str, code=None, **extra) -> Response:
    """An OpenAI-shaped error response.

    Every terminal error the proxy generates itself goes through here, so the
    envelope stays identical across them — clients key off `error.type`, and five
    hand-built literals had already started to drift on whether `code` was an int
    or a string. `code` defaults to the HTTP status; pass a string for the cases
    OpenAI spells out (e.g. `model_not_found`). `extra` adds sibling fields such
    as `param`.

    Not used for relaying an *upstream* error: that body is passed through
    verbatim so the backend's own explanation survives.
    """
    payload = {"error": {"message": message, "type": kind, "code": status if code is None else code}}
    payload["error"].update(extra)
    return Response(
        content=json.dumps(payload), status_code=status, media_type="application/json"
    )


def _relay_headers(headers: dict) -> dict:
    """Upstream response headers, minus the ones that describe a body we changed.

    We decompressed the body and hand it to Starlette to re-frame, so the
    upstream's length, framing and encoding headers now all describe something
    that no longer exists — forwarding any of them corrupts the response.
    """
    out = dict(headers)
    out.pop("content-length", None)
    out.pop("transfer-encoding", None)
    out.pop("content-encoding", None)
    return out


def _backend_error(provider: Provider, model: Optional[str], exc: Exception) -> Response:
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
    if model is not None:
        ERRORS_TOTAL.labels(provider=provider.name, model=model, status_code=str(status)).inc()

    return _error(status, f"Upstream backend '{provider.name}' is unavailable: {exc}", kind)


def _outbound(request: Request, provider: Provider, path: str, body_str: str):
    """The method, URL and headers of the request about to go upstream, logged as
    a curl-like block when LOG_INPUT is on."""
    method = request.method.upper()
    url = _build_url(provider, path)
    headers = _build_headers(provider, request)
    if conf.LOG_INPUT:
        _log_curl(method, url, headers, body_str)
    return method, url, headers


async def _read_body(resp: httpx.Response) -> str:
    """A whole upstream body as text. Reads the raw, undecoded bytes so the
    decompression is ours (see `_decompress`) rather than httpx's."""
    raw = b"".join([chunk async for chunk in resp.aiter_raw()])
    encoding = resp.headers.get("content-encoding", "")
    return _decompress(raw, encoding).decode("utf-8", errors="replace")


def _buffered_response(
    request: Request, provider: Provider, model: Optional[str], resp: httpx.Response,
    resp_body: str, started: float, stream: bool, entry: inflight.Entry,
) -> Response:
    """Account for an upstream response that was read whole, and relay it: every
    non-stream answer, and the error a streaming request got instead of a stream.

    An upstream error keeps its real status and its body, verbatim, so the
    backend's own explanation reaches the client.
    """
    duration = time.time() - started
    status = resp.status_code
    pname = provider.name

    usage = Usage()
    served = None
    try:
        data = json.loads(resp_body)
        usage = parse_usage(data) or usage
        if isinstance(data, dict) and isinstance(data.get("model"), str):
            served = data["model"]
    except ValueError:
        pass

    if resp.is_error:
        _log_upstream_error(pname, model, status, resp_body)
    else:
        _apply_pricing(provider, model, usage, started)
        if conf.LOG_OUTPUT:
            logger.info(f"Response ({status}):\n{_pretty_json(resp_body)}")

    if model is not None:
        if resp.is_error:
            ERRORS_TOTAL.labels(provider=pname, model=model, status_code=str(status)).inc()
        else:
            _record_metrics(pname, model, usage, duration)

    entry.record(status, usage, duration)
    entry.add_response(resp_body)

    return Response(
        content=resp_body,
        status_code=status,
        headers=_relay_headers(dict(resp.headers)),
        background=BackgroundTask(
            _emit_request_log, request, pname, model, status, usage, duration,
            stream, entry, served,
        ),
    )


async def _handle_non_stream(
    request: Request, provider: Provider, path: str, body: bytes, body_str: str,
    model: Optional[str], entry: inflight.Entry,
) -> Response:
    method, url, headers = _outbound(request, provider, path, body_str)
    start = time.time()
    # Connection failures propagate as httpx.RequestError so the dispatcher can
    # fail over to the next backend; the response is fully buffered here. The
    # client is process-wide (see app/upstream.py) so the connection is reused —
    # never close it here.
    async with _client_for(provider).stream(method, url, headers=headers, content=body) as resp:
        resp_body = await _read_body(resp)
    return _buffered_response(request, provider, model, resp, resp_body, start, False, entry)


def _delta_text(value) -> str:
    """The text of one streamed delta field. A string on every OpenAI-style
    backend; a list of content parts on a few, whose `text` fields are joined."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            part["text"] for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


class _StreamTally:
    """What a streamed response said about itself, read off its SSE events as
    they pass: the usage block, the model that answered, and the text.

    Strictly an observer. The bytes have already gone to the client by the time
    `feed` sees them, and nothing a backend sends may end the relay, so `feed`
    never raises: an event it cannot read is skipped.
    """

    def __init__(self, model: Optional[str], entry: inflight.Entry, keep_text: bool):
        self.usage = Usage()
        # What the backend says ran: the id we sent, until an event names another.
        self.model = model
        self._entry = entry
        self._buffer = ""
        self._final_event = None  # the event that carried `usage`
        # Kept only for LOG_OUTPUT; the In-flight row gets its text from `entry`.
        self._text = [] if keep_text else None
        self._reasoning = [] if keep_text else None

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            self._entry.chunk()
            try:
                self._event(json.loads(payload))
            except Exception:  # noqa: BLE001 - an event we cannot read is not the client's problem
                pass

    def _event(self, data) -> None:
        if not isinstance(data, dict):
            return
        if isinstance(data.get("model"), str):
            self.model = data["model"]
        if data.get("usage"):
            # Usually the final chunk; last one wins either way.
            self.usage = parse_usage(data) or self.usage
            self._final_event = data
        choices = data.get("choices") or []
        delta = (choices[0].get("delta") or {}) if choices else {}
        content = _delta_text(delta.get("content"))
        reasoning = _delta_text(delta.get("reasoning_content"))
        if content or reasoning:
            # One delta carrying text == one generation step. The only live
            # token signal there is; see Entry.token.
            self._entry.token()
            self._entry.add_response(content)
            self._entry.add_response(reasoning, reasoning=True)
        if self._text is not None:
            if content:
                self._text.append(content)
            if reasoning:
                self._reasoning.append(reasoning)

    def log_output(self, status: int) -> None:
        """The LOG_OUTPUT record of a finished stream: its final event (or a
        stand-in when no event carried usage) with the assembled text added."""
        if self._text is None:
            return
        record = self._final_event or {
            "model": self.model,
            "usage": {"prompt_tokens": self.usage.prompt, "completion_tokens": self.usage.completion},
        }
        record["_assembled_content"] = "".join(self._text)
        reasoning = "".join(self._reasoning)
        if reasoning:
            record["_reasoning_content"] = reasoning
        logger.info(
            f"Stream response ({status}):\n{json.dumps(record, indent=2, ensure_ascii=False)}"
        )


async def _relay_stream(
    request: Request, provider: Provider, model: Optional[str], resp: httpx.Response,
    started: float, entry: inflight.Entry, close,
):
    """The body of a streaming response: relay the upstream's bytes as they
    arrive, and account for the request when the stream ends, however it ends.

    `close` returns the upstream connection to the pool and releases the slot.
    """
    # Runs in Starlette's body task, not the request task — bind it so a kill
    # cancels the generator rather than the disconnect listener above it.
    entry.bind_stream()
    tally = _StreamTally(model, entry, keep_text=conf.LOG_OUTPUT)
    pname = provider.name
    status = resp.status_code
    error = False

    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
            tally.feed(chunk)
    except asyncio.CancelledError:
        # Killed from the console. The 200 was committed with the first chunk,
        # so there is no status code left to send the client — ending the SSE
        # stream here is all a kill can mean. Returning instead of propagating
        # keeps uvicorn from logging a deliberate operator action as an
        # "Exception in ASGI application" traceback. `error` is set so the
        # truncated generation isn't recorded as throughput metrics.
        error = True
        if not entry.cancelled:
            # Shutdown, or the client vanished — not ours to swallow.
            raise
        logger.info(
            f"Stream for request #{entry.id} cut after {entry.chunks} chunk(s) — cancelled"
        )
    except Exception as e:
        error = True
        logger.error(f"Stream error from '{pname}' (model: {model}): {type(e).__name__}: {e}")
    finally:
        # Bookkeeping first, and all of it sync, so none of it can be skipped
        # by an await below raising. This is the stream's single close point
        # for the in-flight entry: proxy_request hands it off to us, since the
        # handler returns long before the body is done. record-then-finish, so
        # the history row carries the outcome and not just "it ended".
        duration = time.time() - started
        usage = tally.usage
        if not error:
            _apply_pricing(provider, model, usage, started)
        entry.record(status, usage, duration)
        entry.finish()
        await close()
        if model is not None:
            if error:
                ERRORS_TOTAL.labels(provider=pname, model=model, status_code=str(status)).inc()
            else:
                _record_metrics(pname, model, usage, duration)
        await _emit_request_log(
            request, pname, model, status, usage, duration, True, entry, tally.model,
        )
        tally.log_output(status)


async def _handle_stream(
    request: Request, provider: Provider, path: str, body: bytes, body_str: str,
    model: Optional[str], entry: inflight.Entry, on_complete=None,
) -> Union[Response, StreamingResponse]:
    method, url, headers = _outbound(request, provider, path, body_str)

    # Pre-flight the connection: a StreamingResponse commits its status code
    # before the body generator runs, so we must learn whether the backend is
    # reachable *now* — otherwise an offline backend would yield a 200 with an
    # empty body instead of a clean error. A failure here propagates as
    # httpx.RequestError so the dispatcher can fail over before any bytes are
    # committed to the client (failover is impossible mid-stream).
    # The clock starts before the request is sent so duration covers the full
    # exchange, including prompt processing on the backend. A backend that
    # buffers the whole completion before responding (e.g. an aggregator)
    # spends all its time before the first byte — timing only the body read
    # would yield near-zero durations and absurd tokens/sec.
    start = time.time()
    stream_cm = _client_for(provider).stream(method, url, headers=headers, content=body)
    # A failure here — RequestError (the dispatcher fails over on it) or
    # cancellation (an operator kill landing while we connect) — propagates
    # unchanged. There is nothing to clean up: __aenter__ raising means the
    # stream never took a connection from the shared pool, and the client itself
    # is process-wide, so closing it here would break every other request.
    resp = await stream_cm.__aenter__()

    async def close():
        # Closes the response and returns its connection to the shared pool —
        # the client is process-wide, never aclose() it — then gives the slot
        # back, even when closing the response raised.
        try:
            await stream_cm.__aexit__(None, None, None)
        finally:
            if on_complete is not None:
                await on_complete()

    # Upstream rejected the request outright (4xx/5xx). A StreamingResponse
    # commits a 200 before its generator runs, which would bury the error in a
    # bogus SSE stream the client can't interpret. Buffer the (small) error
    # body instead and relay it verbatim with the upstream's real status.
    if resp.is_error:
        try:
            resp_body = await _read_body(resp)
        finally:
            await close()
        return _buffered_response(request, provider, model, resp, resp_body, start, True, entry)

    return StreamingResponse(
        _relay_stream(request, provider, model, resp, start, entry, close),
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
    # Repairs this vendor's API needs on a conversation another backend started (see
    # app/providers/). Per target: the shared payload is never touched, so a failover
    # to a different vendor still gets the client's own history.
    vendor = conf.vendor_of(provider)
    adapt = getattr(get_vendor(vendor), "adapt_request", None) if vendor else None
    if adapt is not None:
        p = adapt(p)
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
    # Drop fields a strict backend would reject (e.g. Google 400s on `num_ctx`).
    for f in provider.strip_fields:
        p.pop(f, None)
    body = json.dumps(p, ensure_ascii=False).encode("utf-8")
    return body, body.decode("utf-8")


def _should_failover(status: int) -> bool:
    """Whether an upstream HTTP error status should trigger trying the next target.

    A backend that *answered* with 503/500/429 is as unusable for this request as
    one we couldn't reach at all, so it gets the same failover treatment as a
    connection error. A deliberate 4xx (bad request, auth) is left out — every
    backend would reject it identically, so relay it instead of burning retries.
    """
    return conf.ROUTING.failover and status in conf.ROUTING.failover_statuses


def _fail_over(target, others: list, why: str) -> None:
    """Record that a request is leaving a backend that failed it for `others`:
    the backend is skipped for `down_backoff`, and the move is counted and logged.

    Only for a request that has somewhere else to go. One that does not is an
    error the client receives, and is counted as that instead.
    """
    registry.mark_down(target.provider, conf.ROUTING.down_backoff)
    FAILOVERS_TOTAL.labels(provider=target.provider).inc()
    logger.warning(
        f"Failover: '{target.provider}' {why}; "
        f"{len(others)} target(s) left for model '{target.model}'"
    )


def _release_once(target):
    """The release of one acquired slot, as a coroutine function that is safe to
    call more than once.

    A slot has several possible releasers: the dispatcher, the stream that outlives
    it, and the error paths of both. An error raised after the stream handler has
    already given the slot back must not release it a second time, which would
    free a slot some other request is holding.
    """
    released = False

    async def release():
        nonlocal released
        if not released:
            released = True
            await slots.release(target.provider, target.model)

    return release


async def _dispatch(
    request: Request, path: str, payload: dict, is_stream: bool, targets: list, entry
) -> Union[Response, StreamingResponse]:
    """Acquire a slot on the best available target and forward, failing over to
    the next-priority target when a backend errors out (when failover is enabled).

    Failover fires on a connection-level `httpx.RequestError` *and* on a retryable
    upstream HTTP status (`Routing.failover_statuses`, e.g. 503), and only while
    another target remains. The last target's failure is what the client gets: an
    upstream error verbatim — its real status and body, not a synthetic 502 — so
    the reason survives, or a 502/504 when the backend could not be reached.
    """
    remaining = list(targets)

    while remaining:
        # Mark queued *before* acquiring: for a request that has to wait, this is
        # the only moment we can record what it is waiting on. Re-armed on every
        # iteration so a failover shows up as queued again, with the shortened
        # candidate list.
        entry.wait(remaining)
        try:
            target = await slots.acquire(
                remaining, conf.ROUTING.queue_timeout, on_skip=entry.passed_over
            )
        except slots.SlotTimeout:
            return _error(
                503, "No backend slot available (queue timeout)", "slot_timeout"
            )

        release = _release_once(target)
        others = [t for t in remaining if t.provider != target.provider]
        provider = conf.PROVIDERS_BY_NAME.get(target.provider)
        if provider is None:
            # The config hot-reloaded away this provider between resolution and
            # admission — genuinely possible, since the console can delete a
            # backend while a request is queued for it. Give the slot back and
            # move on rather than raising a KeyError into the client.
            await release()
            remaining = others
            logger.warning(
                "Provider '%s' disappeared from the config while a request was "
                "queued for it; %d target(s) left for model '%s'",
                target.provider, len(remaining), target.model,
            )
            continue
        entry.run(target.provider, target.model)

        try:
            body, body_str = _build_body(payload, provider, target.model)
            if is_stream:
                # Slot is released by the stream's own close once it ends, or — on
                # a pre-first-byte error — via on_complete inside the handler.
                resp = await _handle_stream(
                    request, provider, path, body, body_str, target.model, entry,
                    on_complete=release,
                )
            else:
                resp = await _handle_non_stream(
                    request, provider, path, body, body_str, target.model, entry
                )
                await release()
        except httpx.RequestError as e:
            await release()
            if conf.ROUTING.failover and others:
                _fail_over(target, others, f"failed ({type(e).__name__})")
                remaining = others
                continue
            # Nowhere else to go, so this failure is the client's answer. The
            # backend is still marked down: the next request for a model it
            # shares with others should not walk into the same wall.
            if conf.ROUTING.failover:
                registry.mark_down(target.provider, conf.ROUTING.down_backoff)
            return _backend_error(provider, target.model, e)
        except BaseException:
            # An operator kill or a client disconnect while we were forwarding
            # (CancelledError), or a bug. Whatever it is, the slot goes back
            # before it unwinds: the non-stream branch never reached its release,
            # and a stream that never started has no generator to do it.
            await release()
            raise

        if resp.status_code == 402:
            # Payment required: the backend's balance ran out between two reads. The
            # failover below moves this request on; the warning is fire-and-forget.
            notify.payment_required(target.provider, target.model)

        # The backend answered with a retryable error status (the connection was
        # fine, the response wasn't). Try the next target if any remain; otherwise
        # fall through and relay this error to the client verbatim.
        if _should_failover(resp.status_code) and others:
            _fail_over(target, others, f"returned {resp.status_code}")
            remaining = others
            continue

        # A genuinely good response clears any down-mark; an error we're relaying
        # (terminal, no targets left) must not — the backend is still unhealthy.
        if resp.status_code < 400:
            registry.clear_down(target.provider)
        return resp

    # Only reachable when every candidate's provider left the config while this
    # request waited for it: there is no backend to name, and no upstream answer
    # to relay.
    return _error(
        502, "No backend is left to serve this request (its provider was removed "
        "from the configuration)", "upstream_unavailable",
    )


def _cancelled_response() -> Response:
    """Terminal response for a request killed from the console. 503 with an
    OpenAI-shaped error body, matching the slot-timeout response, so clients
    handle it as an ordinary upstream failure rather than a protocol surprise."""
    return _error(503, "Request cancelled by the proxy operator", "cancelled")


def _model_not_found(model: str, request: Request) -> Response:
    """404 for a model no backend is known to serve.

    Shaped like OpenAI's own unknown-model error (`invalid_request_error` /
    `model_not_found`) so off-the-shelf clients surface the message instead of
    showing a bare failure. Logged with the caller, because the interesting
    question is always *who* asked for a name the proxy doesn't have — a typo, a
    stale client-side model list, or a backend that dropped out of discovery.
    """
    logger.warning(
        "Model '%s' is not available — no backend serves it (client %s, svc %s). "
        "Returning 404 rather than guessing a backend.",
        model, clientinfo.client_ip(request),
        clientinfo.service_from_ua(clientinfo.user_agent(request)),
    )
    return _error(
        404,
        f"The model '{model}' does not exist or is not available on this proxy",
        "invalid_request_error",
        code="model_not_found",
        param="model",
    )


def _unauthorized(model: str) -> Response:
    return _error(
        401,
        f"Model '{model}' requires authentication" if model else "Authentication required",
        "unauthorized",
    )


async def proxy_request(request: Request, path: str) -> Union[Response, StreamingResponse]:
    """Entry point for every proxied call. Parses the body once, registers the
    request in the in-flight view, then hands off to `_route`.

    The in-flight entry is closed here for anything that returns a finished
    response. A `StreamingResponse` is the one exception: it is still in flight
    when we return it, so `_relay_stream` closes the entry in its `finally` — the
    same place the slot is released.
    """
    body = await request.body()
    body_str = body.decode("utf-8", errors="replace")

    payload = None
    model = None
    is_stream = False
    try:
        payload = json.loads(body_str)
        model = payload.get("model") or None
        is_stream = bool(payload.get("stream", False))
    except (json.JSONDecodeError, AttributeError):
        # Not a JSON object (or not JSON at all): treated as a passthrough below.
        payload = None

    entry = inflight.begin(
        model=model,
        stream=is_stream,
        op=_op_kind(request.url.path, model or ""),
        method=request.method,
        path=request.url.path,
        req_bytes=len(body),
        client_ip=clientinfo.client_ip(request),
        svc=clientinfo.service_from_ua(clientinfo.user_agent(request)),
    )
    entry.set_request(body_str)
    try:
        resp = await _route(request, path, body, body_str, payload, model, is_stream, entry)
    except asyncio.CancelledError:
        entry.finish()
        if not entry.cancelled:
            # The client went away (or we're shutting down): normal cancellation,
            # let it propagate untouched.
            raise
        # Killed from the console. Answer the client rather than letting uvicorn
        # turn the cancellation into a 500 with a traceback. Safe to stop the
        # unwind here: the cancellation has already been delivered, so the awaits
        # needed to send this response still run.
        logger.warning(
            f"Request #{entry.id} ({entry.model or entry.path}"
            f"{' on ' + entry.provider if entry.provider else ''}) cancelled from the console"
        )
        return _cancelled_response()
    except BaseException:
        entry.finish()
        raise
    if not isinstance(resp, StreamingResponse):
        entry.finish(fallback_status=resp.status_code)
    return resp


async def _passthrough(
    request: Request, path: str, body: bytes, body_str: str, is_stream: bool,
    authorized: bool, entry,
) -> Union[Response, StreamingResponse]:
    """Forward a request that names no model (not a chat body, nothing to
    resolve) untouched, with no slot and no failover.

    It goes to the first backend the caller is *permitted* to use, not the first
    configured one: picking `PROVIDERS[0]` blindly gave an unauthenticated caller
    "Authentication required" whenever the config happened to list a paid backend
    first — a 401 about a backend they never chose. HTTP backends only: an
    arbitrary path is forwarded as it came, and the CLI answers nothing but chat
    completions.
    """
    provider = next(
        (
            p for p in conf.PROVIDERS
            if (authorized or not p.require_permission) and p.kind == conf.KIND_HTTP
        ),
        None,
    )
    if provider is None:
        return _unauthorized("")
    # No slot is taken on this path, so it is never queued — running from the
    # moment it is forwarded.
    entry.run(provider.name, None)
    handler = _handle_stream if is_stream else _handle_non_stream
    try:
        return await handler(request, provider, path, body, body_str, None, entry)
    except httpx.RequestError as e:
        return _backend_error(provider, None, e)


async def _route(
    request: Request, path: str, body: bytes, body_str: str, payload,
    model: Optional[str], is_stream: bool, entry,
) -> Union[Response, StreamingResponse]:
    """Resolve the model, apply the auth gate, pick the order and dispatch.
    `payload` is the already-parsed JSON body (None for a non-JSON passthrough)
    and `model` the name it asked for (None when it names none)."""
    if not conf.PROVIDERS:
        return _error(503, "No providers configured", "no_providers")
    authorized = auth.is_authorized(request)
    if not model:
        return await _passthrough(request, path, body, body_str, is_stream, authorized, entry)

    # Nothing serves this model: say so — never fall back to an arbitrary
    # backend, which is how a local-model request used to surface as a 401
    # from a paid one.
    targets = await router.resolve(model)
    if not targets:
        return _model_not_found(model, request)

    # Gate: unauthenticated callers can only reach open backends. If the model
    # lives solely behind permission-required backends, reject with 401.
    if not authorized:
        targets = [t for t in targets if not auth.restricted(t.provider)]
        if not targets:
            return _unauthorized(model)

    # Prefer backends that are up and have money, but keep the rest as a last resort: a
    # backend marked down may have recovered, and a balance is only as fresh as the last
    # read, so something we believe is empty is still better than refusing the request.
    healthy = [t for t in targets if not registry.is_down(t.provider)]
    funded = [t for t in healthy if not rates.out_of_funds(t.provider)]
    candidates = funded or healthy or targets

    # Smart routing (app/smart.py): cheapest backend first, when switched on, and
    # the operator's `first`/`last` overrides, always. A reorder of the candidates
    # already chosen above — it cannot add a backend the gate or the resolver
    # left out — and a no-op unless one of those is in force.
    candidates, routed = smart.order(candidates, model)
    if routed is not None:
        entry.mark_routed(routed)

    # Context guardrail: a conversation the client says cannot fit (`num_ctx`)
    # is shrunk once here, before any target is chosen, so every failover
    # attempt forwards the same trimmed body. Fits → `payload` is untouched.
    # In a thread: tokenizing an agent's whole history takes tens of ms (more on
    # the first, which loads the tokenizer), and the single event loop is
    # relaying every other stream meanwhile. The tokenizer runs without the GIL.
    trimmed = await asyncio.to_thread(trim.trim_request, payload, body_str, model, entry.id)
    if trimmed is not None:
        payload = trimmed.payload
        entry.mark_trimmed(trimmed.as_dict())

    return await _dispatch(request, path, payload, is_stream, candidates, entry)
