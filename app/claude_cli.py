"""The `claude-cli` provider kind: OpenAI chat completions answered by the official
Claude Code CLI, run headless (`claude -p`).

## Why a transport

Everything the proxy does around a request — slot accounting, failover, metrics,
the `event=request` line, the In-flight row and its Kill button — hangs off one
forwarding path through an httpx client. So the CLI plugs in *underneath* that
path, as an `httpx.AsyncBaseTransport` (the same seam the test suite's
MockTransport uses): `proxy` hands a claude-cli provider this module's client
instead of the shared HTTP one, and nothing else in the lifecycle can tell the
difference. The transport keeps the contract a real backend has: a request the
CLI answered with an error comes back as that HTTP status (429/5xx fail over like
any backend's), and one it never answered — the binary would not start, or ran
past the read timeout — raises `httpx.RequestError`, which the dispatcher already
treats as a connection failure.

## Why the CLI, and nothing lower

Authentication belongs to the CLI. It is logged in with the operator's own Claude
subscription through Anthropic's own flow (`claude auth login`), and this module
never reads, copies or forwards those credentials — it only runs the unmodified
binary. Lifting the subscription token into another client is what Anthropic's
terms forbid; running the binary as published is what they allow.

## What it is not

An agent. The CLI ships file, shell and web tools, and the proxy's container
holds a config with every other backend's API key, so a prompt must not be able
to reach any of them: tools, MCP servers, settings files, slash commands and
session persistence are all switched off, and the process runs in an empty
scratch directory. A request that needs images or audio is refused with a 400
rather than quietly degraded into something it did not ask for.

## Function calling, emulated

A client's `tools` cannot reach the CLI as tools: it only takes tools through
MCP, and in the OpenAI protocol it is the *client* that runs a call, in a later
request. So the functions travel as text, listed in the system prompt, and the
model calls them as tools anyway — the CLI has no such tool, but the call is
complete in its event stream (`stream-json`) before the CLI can answer "no such
tool". `_Watch` catches it there, the process is killed, and the call goes back
out as OpenAI `tool_calls`. Earlier calls and their results come back in as part
of the transcript. The CLI's own tool list stays empty throughout: the model can
only *ask* for a call, and only the client can make one. A run that calls nothing
answers through structured output (`--json-schema`), which is there to switch
tool use on at all (see `_REPLY`).

Streaming is synthesized: the CLI runs to completion and the answer goes out as a
single SSE chunk, so streaming clients work but see the reply arrive all at once.
"""
import asyncio
import json
import math
import os
import tempfile
import time
import uuid
from typing import Dict, List, NamedTuple, Optional, Tuple

import httpx

from app import config as conf
from app import upstream

# The binary, as argv. A list so the tests can run a stand-in via the interpreter.
COMMAND = ["claude"]

# Used when the client sends no system prompt. Without one the CLI falls back to
# Claude Code's own coding-agent prompt — the wrong persona for a chat request.
DEFAULT_SYSTEM = "You are Claude, a helpful AI assistant."

# `reasoning_effort` values the CLI's --effort accepts; anything else is ignored.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Turns the agent into a plain completion: no tools, no MCP servers, no settings
# or hooks read from disk, no slash commands, nothing saved to resume later.
_LOCKDOWN = [
    "--tools", "",
    "--strict-mcp-config",
    "--setting-sources", "",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--permission-prompts", "none",
]

_workdir_path: Optional[str] = None
_client: Optional[httpx.AsyncClient] = None


class Unsupported(ValueError):
    """A request this backend cannot serve faithfully. Becomes a 400."""


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
                continue
            kind = part.get("type") if isinstance(part, dict) else type(part).__name__
            raise Unsupported(f"content of type '{kind}' is not supported: this backend is text-only")
        return "\n".join(parts)
    raise Unsupported("message content must be a string or a list of text parts")


_LEGACY = "legacy `functions`/`function_call` calling is not supported by this backend; send `tools`"

# Appended to the system prompt when the request offers functions. It invites the
# model to call them as tools, which is what it does anyway: told instead to
# request calls through a field of its structured reply, it still invoked them
# directly, hit "no such tool", and gave up on the call.
_FUNCTIONS = """\
# Functions

You can call the functions listed below as tools. Invoke one with its arguments
and the application you are running in runs it, then continues this
conversation with the result, so never guess a result: wait for it. Call a
function only when the reply needs it.

Calls made earlier appear in the conversation as <function_call> and
<function_result>. Never write a call out as text like that: invoke the tool.

Your reply to the user goes in the `content` of your structured output."""

# The reply schema (`--json-schema`). It is needed less for its shape than for what
# it switches on: with every CLI tool off, the API request declares no tools, and
# then the model never emits a real call — it writes one out as text and invents
# the result. Structured output declares one tool, and with it tool use.
_REPLY = json.dumps({
    "type": "object",
    "properties": {"content": {"type": "string"}},
    "required": ["content"],
}, separators=(",", ":"))


class Translated(NamedTuple):
    system: str
    prompt: str
    # The functions the model may call this turn, name -> parameters schema;
    # empty for a plain chat.
    functions: Dict[str, dict] = {}
    # parallel_tool_calls: false — at most one call per turn.
    single: bool = False


def _offer(payload: dict) -> Tuple[str, Dict[str, dict], bool]:
    """The functions a request offers -> (system prompt addendum, callable
    functions, one call at most); ("", {}, False) when it offers none, or forbids
    calling them.

    The client's parameter schemas go into the prompt as they are, never into a
    validated schema: they are of any size and dialect, and a validator rejecting
    one would fail the whole request where a real backend would simply pass a
    slightly wrong argument along.
    """
    if payload.get("functions") or payload.get("function_call"):
        raise Unsupported(_LEGACY)
    tools, choice = payload.get("tools"), payload.get("tool_choice")
    if not tools or choice == "none":
        return "", {}, False
    if not isinstance(tools, list):
        raise Unsupported("'tools' must be a list")
    functions = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            kind = tool.get("type") if isinstance(tool, dict) else type(tool).__name__
            raise Unsupported(f"tool of type '{kind}' is not supported: this backend calls functions only")
        fn = tool.get("function")
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
            raise Unsupported("every tool needs a function name")
        functions.append({
            "name": fn["name"],
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    offered = {f["name"]: f["parameters"] for f in functions}
    if len(offered) != len(functions):
        raise Unsupported("tool function names must be unique")

    # A call can't be forced, only asked for: the model calls of its own accord.
    # What the transport does enforce is which calls go back to the client.
    notes = []
    if isinstance(choice, dict):
        forced = (choice.get("function") or {}).get("name") if choice.get("type") == "function" else None
        if forced not in offered:
            raise Unsupported("tool_choice must name a function listed in 'tools'")
        offered = {forced: offered[forced]}
        notes.append(f"This turn, call `{forced}`.")
    elif choice == "required":
        notes.append("This turn, call at least one function.")
    elif choice not in (None, "auto"):
        raise Unsupported(f"unsupported tool_choice {choice!r}")
    single = payload.get("parallel_tool_calls") is False
    if single:
        notes.append("Call one function at a time.")

    listing = "\n".join(json.dumps(f, ensure_ascii=False) for f in functions)
    addendum = "\n\n".join([_FUNCTIONS, *notes, f"<functions>\n{listing}\n</functions>"])
    return addendum, offered, single


def _attrs(**values) -> str:
    return "".join(f" {k}={json.dumps(v, ensure_ascii=False)}" for k, v in values.items())


def _calls(tool_calls, called: dict) -> str:
    """An assistant turn's `tool_calls` as transcript lines, remembering each
    call's function name by id for the results that answer it."""
    if not isinstance(tool_calls, list):
        raise Unsupported("'tool_calls' must be a list")
    lines = []
    for call in tool_calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise Unsupported("every tool call needs a function name")
        ref = str(call.get("id") or "")
        called[ref] = fn["name"]
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {}, ensure_ascii=False)
        lines.append(f"<function_call{_attrs(id=ref, name=fn['name'])}>{args}</function_call>")
    return "\n".join(lines)


def translate(payload: dict) -> Translated:
    """An OpenAI chat body -> what `claude -p` is handed.

    Leading system/developer messages become the system prompt. A conversation
    that is a single user message is sent verbatim. Anything longer — history, or
    a system note partway through it (SillyTavern's author's notes) — is rendered
    as a role-tagged transcript, because `-p` takes exactly one prompt: there is
    no way to hand the CLI earlier assistant turns as turns. Function calls and
    their results are rendered into the same transcript.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise Unsupported("'messages' must be a non-empty list")
    addendum, functions, single = _offer(payload)

    system, turns, called = [], [], {}
    for m in messages:
        if not isinstance(m, dict):
            raise Unsupported("every message must be an object")
        role = m.get("role")
        if role == "function" or m.get("function_call"):
            raise Unsupported(_LEGACY)
        if role not in ("system", "developer", "user", "assistant", "tool"):
            raise Unsupported(f"unsupported message role '{role}'")
        text = _text(m.get("content"))
        if role in ("system", "developer") and not turns:
            system.append(text)
        elif role == "assistant" and m.get("tool_calls"):
            turns.append(("assistant", "", "\n".join(filter(None, [text, _calls(m["tool_calls"], called)]))))
        elif role == "tool":
            ref = str(m.get("tool_call_id") or "")
            turns.append(("function_result", _attrs(id=ref, name=called.get(ref, "")), text))
        else:
            turns.append(("system" if role == "developer" else role, "", text))
    if not turns:
        raise Unsupported("the conversation has no user message")

    system_prompt = "\n\n".join(s for s in system if s.strip()) or DEFAULT_SYSTEM
    if addendum:
        system_prompt = f"{system_prompt}\n\n{addendum}"
    if len(turns) == 1 and turns[0][0] == "user":
        return Translated(system_prompt, turns[0][2], functions, single)
    transcript = "\n".join(f"<{tag}{attrs}>\n{text}\n</{tag}>" for tag, attrs, text in turns)
    ask = (
        "Take the assistant's next step in the conversation above: call the functions "
        "it needs, or write its message, without role tags."
        if functions else
        "Write the assistant's next message in the conversation above. "
        "Reply with the message text only, without role tags."
    )
    return Translated(system_prompt, f"<conversation>\n{transcript}\n</conversation>\n\n{ask}", functions, single)


def _workdir() -> str:
    """An empty directory to run the CLI in, so it finds no CLAUDE.md or project
    settings to pull into the prompt. Created once per process."""
    global _workdir_path
    if _workdir_path is None or not os.path.isdir(_workdir_path):
        _workdir_path = tempfile.mkdtemp(prefix="llm-proxy-claude-")
    return _workdir_path


def _argv(model: str, system_file: str, payload: dict, functions: bool = False) -> List[str]:
    # The system prompt goes through a file and the prompt through stdin: Linux
    # caps a single argv string at 128 KiB, which a character card or a long chat
    # history clears easily, and argv is visible to anyone running `ps`.
    argv = [
        *COMMAND, "-p", "--model", model, "--system-prompt-file", system_file, *_LOCKDOWN,
    ]
    if functions:
        # Event by event, so a call can be caught the moment the model makes it.
        argv += ["--output-format", "stream-json", "--verbose", "--json-schema", _REPLY]
    else:
        argv += ["--output-format", "json"]
    effort = payload.get("reasoning_effort")
    if effort in EFFORTS:
        argv += ["--effort", effort]
    return argv


class _Watch:
    """Catches the model calling one of the client's functions in a stream-json run.

    The model invokes the functions as tools of its own. The CLI has no such tools,
    so it would answer "no such tool" and the model would carry on without the
    call — but by then the call is complete, arguments and all, in the event
    stream. So the watch collects it there and ends the run before the model ever
    reads that error; the call goes back to the client as `tool_calls`.
    """

    def __init__(self, functions: Dict[str, dict]):
        self.functions = functions
        self.calls: List[dict] = []
        self.text: List[str] = []
        self.message: dict = {}
        self._id = None

    def __call__(self, line: bytes) -> bool:
        """Feed one stdout line; True once the run has what it needs."""
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict):
            return False
        if event.get("type") != "assistant":
            # The CLI answers a message's tool calls only once the message is
            # complete, so its reply means every call that message carried is in.
            return bool(self.calls) and event.get("type") == "user"
        message = event.get("message") or {}
        if message.get("id") != self._id:
            # The CLI prints a message block by block; text is kept per message.
            self._id, self.text = message.get("id"), []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                self.text.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use" and block.get("name") in self.functions:
                self.calls.append(block)
                self.message = message
        return False


# A stream-json line carries a whole message; asyncio's default 64 KiB line
# limit would fail any long answer.
_LINE_LIMIT = 64 * 1024 * 1024


async def _communicate(proc, data: bytes, watch: Optional[_Watch]) -> Tuple[bytes, bytes]:
    """`proc.communicate(data)` — except with a watch, stdout is read line by line
    and reading stops the moment the watch has what it needs. The caller then
    kills the process, which is still running."""
    if watch is None:
        return await proc.communicate(data)
    stderr = asyncio.ensure_future(proc.stderr.read())
    try:
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # it exited before reading the prompt; its output says why
        lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if watch(line):
                return b"".join(lines), b""
        await proc.wait()
        return b"".join(lines), await stderr
    finally:
        if not stderr.done():
            stderr.cancel()


async def _run(
    argv: List[str], prompt: str, timeout, request: httpx.Request, watch: Optional[_Watch] = None,
) -> Tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_workdir(),
            limit=_LINE_LIMIT,
        )
    except OSError as e:
        # Missing or non-executable binary: this backend cannot answer at all,
        # which to failover is exactly a connection failure.
        raise httpx.ConnectError(f"cannot start the claude CLI: {e}", request=request) from e
    try:
        out, err = await asyncio.wait_for(_communicate(proc, prompt.encode("utf-8"), watch), timeout)
    except asyncio.TimeoutError:
        raise httpx.ReadTimeout(
            f"the claude CLI gave no answer within {timeout:.0f}s", request=request
        ) from None
    finally:
        # Timeout, a caught call, operator kill or client disconnect: the slot is
        # about to be released, so the process that was using it must not outlive it.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode, out, err


def _result(out: bytes) -> Optional[dict]:
    """The `type: result` object `--output-format json` prints. The CLI can emit
    diagnostic lines ahead of it (`[claude-code:unrecognized_model] {...}`), so
    take the last line that parses as one rather than the whole of stdout."""
    for line in reversed(out.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


def _status(result: dict) -> int:
    """The HTTP status to answer a failed CLI run with."""
    status = result.get("api_error_status")
    if not isinstance(status, int) or not 400 <= status <= 599:
        return 502
    if status in (401, 403):
        # The CLI's own login was refused (expired, revoked). Relaying the 401
        # would tell the caller that *their* proxy key is wrong.
        return 502
    if status == 529:
        # Anthropic's "overloaded": what 503 means, and 503 is what failover knows.
        return 503
    return status


def _envelope(ident: str, model: str, message: dict, finish: str, usage: dict) -> dict:
    # Cached prompt tokens are still prompt tokens; the CLI reports them apart.
    # They are kept apart too, in the OpenAI `prompt_tokens_details` spelling the
    # proxy's usage parser reads, so the Costs tab's cache figures cover this
    # backend as well. No `cost`: the CLI's `total_cost_usd` is a list-price
    # estimate of usage drawn from a subscription seat, not a charge, and
    # reporting it as one would make the ledger lie.
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    prompt_tokens = int(usage.get("input_tokens") or 0) + cache_read + cache_write
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "id": f"chatcmpl-{ident}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {
                "cached_tokens": cache_read,
                "cache_write_tokens": cache_write,
            },
        },
    }


def _completion(result: dict, model: str, structured: bool = False) -> dict:
    """A finished run's `result` -> an OpenAI completion."""
    text = result.get("result") or ""
    reply = result.get("structured_output") if structured else None
    if isinstance(reply, dict) and isinstance(reply.get("content"), str):
        text = reply["content"]
    # Anything else — no structured reply at all — is answered in prose: still an answer.
    by_model = result.get("modelUsage") or {}
    return _envelope(
        result.get("session_id") or uuid.uuid4().hex,
        # The concrete model behind the alias, when the CLI reports it.
        max(by_model, key=lambda k: (by_model[k] or {}).get("outputTokens", 0)) if by_model else model,
        {"role": "assistant", "content": text},
        "length" if result.get("stop_reason") == "max_tokens" else "stop",
        result.get("usage") or {},
    )


def _scalar(value, kind):
    """One argument put into the scalar type its schema asks for, when it converts
    cleanly; otherwise as it was."""
    try:
        if isinstance(value, str):
            text = value.strip()
            if kind == "integer":
                return int(text)
            if kind == "number":
                number = int(text) if text.lstrip("-").isdigit() else float(text)
                return number if math.isfinite(number) else value
            if kind == "boolean" and text.lower() in ("true", "false"):
                return text.lower() == "true"
        elif kind == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    except ValueError:
        pass
    return value


def _coerce(args, parameters) -> dict:
    """A call's arguments, with the scalar types the model got loosely wrong put
    right against the client's own parameter schema (`"5"` for an integer).

    The functions reach the model as prompt text, not as declared tools, so
    nothing shaped these arguments by their schema the way a native call is — and
    a client's typed handler can fail on a string where it expects a count.
    Top-level properties only; anything that does not convert cleanly is left alone.
    """
    if not isinstance(args, dict):
        return {}
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(properties, dict):
        return args
    return {
        key: _scalar(value, (properties.get(key) or {}).get("type") if isinstance(properties.get(key), dict) else None)
        for key, value in args.items()
    }


def _caught(watch: _Watch, model: str, single: bool) -> dict:
    """Calls the watch caught -> an OpenAI completion that asks the client to run them.

    Usage is the message's as the CLI printed it mid-stream, before the message
    ended: the prompt side is exact, the output side can undercount a little.
    """
    calls = [
        {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": block["name"],
                "arguments": json.dumps(
                    _coerce(block.get("input"), watch.functions.get(block["name"])), ensure_ascii=False
                ),
            },
        }
        for block in (watch.calls[:1] if single else watch.calls)
    ]
    message = {"role": "assistant", "content": "\n".join(watch.text).strip() or None, "tool_calls": calls}
    return _envelope(
        uuid.uuid4().hex, watch.message.get("model") or model, message, "tool_calls", watch.message.get("usage") or {},
    )


def _sse(completion: dict) -> bytes:
    """A finished completion as an OpenAI chat stream: the whole text in one
    delta, then the finish reason, then usage (what `include_usage` asks for)."""
    head = {k: completion[k] for k in ("id", "created", "model")}
    head["object"] = "chat.completion.chunk"
    choice = completion["choices"][0]
    message = choice["message"]
    delta = {"role": "assistant", "content": message["content"]}
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
    chunks = [
        {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
        {**head, "choices": [], "usage": completion["usage"]},
    ]
    events = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    events.append("data: [DONE]\n\n")
    return "".join(events).encode("utf-8")


def _response(status: int, body: bytes, content_type: str) -> httpx.Response:
    # A stream, not `content=`: the proxy reads every body with aiter_raw().
    return httpx.Response(status, stream=httpx.ByteStream(body), headers={"content-type": content_type})


def _answer(completion: dict, payload: dict) -> httpx.Response:
    if payload.get("stream"):
        return _response(200, _sse(completion), "text/event-stream")
    return _response(200, json.dumps(completion, ensure_ascii=False).encode("utf-8"), "application/json")


def _error(status: int, message: str, kind: str) -> httpx.Response:
    body = json.dumps({"error": {"message": message, "type": kind, "code": status}})
    return _response(status, body.encode("utf-8"), "application/json")


class Transport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            return _error(
                404,
                f"this backend only serves POST /v1/chat/completions, not {request.method} {request.url.path}",
                "invalid_request_error",
            )
        try:
            payload = json.loads(await request.aread())
            if not isinstance(payload, dict):
                raise ValueError(payload)
            translated = translate(payload)
        except Unsupported as e:
            return _error(400, str(e), "invalid_request_error")
        except ValueError:
            return _error(400, "the request body is not a JSON object", "invalid_request_error")

        model = str(payload.get("model") or "")
        # The client's read timeout (FORWARD_TIMEOUT): there is no socket to
        # enforce it, so the transport does.
        timeout = (request.extensions.get("timeout") or {}).get("read")
        fd, system_file = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=_workdir())
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(translated.system)
            functions = bool(translated.functions)
            watch = _Watch(translated.functions) if functions else None
            argv = _argv(model, system_file, payload, functions)
            code, out, err = await _run(argv, translated.prompt, timeout, request, watch)
        finally:
            os.unlink(system_file)

        if watch is not None and watch.calls:
            return _answer(_caught(watch, model, translated.single), payload)

        result = _result(out)
        if result is None:
            detail = (err or out).decode("utf-8", errors="replace").strip()[-2000:]
            return _error(
                502, f"the claude CLI exited {code} without a result: {detail or '(no output)'}", "upstream_error"
            )
        if result.get("is_error"):
            message = str(result.get("result") or result.get("subtype") or "the claude CLI reported an error")
            return _error(_status(result), message, "upstream_error")

        return _answer(_completion(result, model, functions), payload)


def client() -> httpx.AsyncClient:
    """The client `proxy` sends a claude-cli provider's requests through.

    Carries FORWARD_TIMEOUT so the CLI gets the same long read budget as any
    backend (see app/upstream.py). `base_url` only gives the path-only URL such a
    provider builds — it has no endpoint of its own — a scheme httpx accepts.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            transport=Transport(), base_url="http://claude-cli", timeout=upstream.FORWARD_TIMEOUT
        )
    return _client
