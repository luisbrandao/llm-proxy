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
scratch directory. A request that needs tools, images or audio is refused with a
400 rather than quietly degraded into something it did not ask for.

Streaming is synthesized: the CLI runs to completion and the answer goes out as a
single SSE chunk, so streaming clients work but see the reply arrive all at once.
"""
import asyncio
import json
import os
import tempfile
import time
import uuid
from typing import List, Optional, Tuple

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


def translate(payload: dict) -> Tuple[str, str]:
    """An OpenAI chat body -> (system prompt, prompt) for `claude -p`.

    Leading system/developer messages become the system prompt. A conversation
    that is a single user message is sent verbatim. Anything longer — history, or
    a system note partway through it (SillyTavern's author's notes) — is rendered
    as a role-tagged transcript, because `-p` takes exactly one prompt: there is
    no way to hand the CLI earlier assistant turns as turns.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise Unsupported("'messages' must be a non-empty list")
    if (payload.get("tools") or payload.get("functions")) and payload.get("tool_choice") != "none":
        raise Unsupported("tool calling is not supported by this backend")

    system, turns = [], []
    for m in messages:
        if not isinstance(m, dict):
            raise Unsupported("every message must be an object")
        role = m.get("role")
        if role in ("tool", "function") or m.get("tool_calls") or m.get("function_call"):
            raise Unsupported("tool calls in the conversation are not supported by this backend")
        if role not in ("system", "developer", "user", "assistant"):
            raise Unsupported(f"unsupported message role '{role}'")
        text = _text(m.get("content"))
        if role in ("system", "developer") and not turns:
            system.append(text)
        else:
            turns.append(("system" if role == "developer" else role, text))
    if not turns:
        raise Unsupported("the conversation has no user message")

    system_prompt = "\n\n".join(s for s in system if s.strip()) or DEFAULT_SYSTEM
    if len(turns) == 1 and turns[0][0] == "user":
        return system_prompt, turns[0][1]
    transcript = "\n".join(f"<{role}>\n{text}\n</{role}>" for role, text in turns)
    prompt = (
        f"<conversation>\n{transcript}\n</conversation>\n\n"
        "Write the assistant's next message in the conversation above. "
        "Reply with the message text only, without role tags."
    )
    return system_prompt, prompt


def _workdir() -> str:
    """An empty directory to run the CLI in, so it finds no CLAUDE.md or project
    settings to pull into the prompt. Created once per process."""
    global _workdir_path
    if _workdir_path is None or not os.path.isdir(_workdir_path):
        _workdir_path = tempfile.mkdtemp(prefix="llm-proxy-claude-")
    return _workdir_path


def _argv(model: str, system_file: str, payload: dict) -> List[str]:
    # The system prompt goes through a file and the prompt through stdin: Linux
    # caps a single argv string at 128 KiB, which a character card or a long chat
    # history clears easily, and argv is visible to anyone running `ps`.
    argv = [
        *COMMAND, "-p", "--output-format", "json", "--model", model,
        "--system-prompt-file", system_file, *_LOCKDOWN,
    ]
    effort = payload.get("reasoning_effort")
    if effort in EFFORTS:
        argv += ["--effort", effort]
    return argv


async def _run(argv: List[str], prompt: str, timeout, request: httpx.Request) -> Tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_workdir(),
        )
    except OSError as e:
        # Missing or non-executable binary: this backend cannot answer at all,
        # which to failover is exactly a connection failure.
        raise httpx.ConnectError(f"cannot start the claude CLI: {e}", request=request) from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout)
    except asyncio.TimeoutError:
        raise httpx.ReadTimeout(
            f"the claude CLI gave no answer within {timeout:.0f}s", request=request
        ) from None
    finally:
        # Timeout, operator kill or client disconnect: the slot is about to be
        # released, so the process that was using it must not outlive it.
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


def _completion(result: dict, model: str) -> dict:
    usage = result.get("usage") or {}
    # Cached prompt tokens are still prompt tokens; the CLI reports them apart.
    prompt_tokens = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    completion_tokens = int(usage.get("output_tokens") or 0)
    by_model = result.get("modelUsage") or {}
    return {
        "id": f"chatcmpl-{result.get('session_id') or uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        # The concrete model behind the alias, when the CLI reports it.
        "model": max(by_model, key=lambda k: (by_model[k] or {}).get("outputTokens", 0)) if by_model else model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.get("result") or ""},
            "finish_reason": "length" if result.get("stop_reason") == "max_tokens" else "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _sse(completion: dict) -> bytes:
    """A finished completion as an OpenAI chat stream: the whole text in one
    delta, then the finish reason, then usage (what `include_usage` asks for)."""
    head = {k: completion[k] for k in ("id", "created", "model")}
    head["object"] = "chat.completion.chunk"
    choice = completion["choices"][0]
    chunks = [
        {**head, "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": choice["message"]["content"]},
            "finish_reason": None,
        }]},
        {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
        {**head, "choices": [], "usage": completion["usage"]},
    ]
    events = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    events.append("data: [DONE]\n\n")
    return "".join(events).encode("utf-8")


def _response(status: int, body: bytes, content_type: str) -> httpx.Response:
    # A stream, not `content=`: the proxy reads every body with aiter_raw().
    return httpx.Response(status, stream=httpx.ByteStream(body), headers={"content-type": content_type})


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
            system, prompt = translate(payload)
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
                f.write(system)
            code, out, err = await _run(_argv(model, system_file, payload), prompt, timeout, request)
        finally:
            os.unlink(system_file)

        result = _result(out)
        if result is None:
            detail = (err or out).decode("utf-8", errors="replace").strip()[-2000:]
            return _error(
                502, f"the claude CLI exited {code} without a result: {detail or '(no output)'}", "upstream_error"
            )
        if result.get("is_error"):
            message = str(result.get("result") or result.get("subtype") or "the claude CLI reported an error")
            return _error(_status(result), message, "upstream_error")

        completion = _completion(result, model)
        if payload.get("stream"):
            return _response(200, _sse(completion), "text/event-stream")
        return _response(200, json.dumps(completion, ensure_ascii=False).encode("utf-8"), "application/json")


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
