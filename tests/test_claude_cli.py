"""The claude-cli backend: an OpenAI chat body in, `claude -p` run, an OpenAI
completion out — through the real proxy lifecycle.

The CLI is replaced by `FAKE`, a small script run with this interpreter, so the
real subprocess path (spawn, stdin, the system-prompt file, timeout, kill) runs
with no network and no Claude account. It records what it was handed and answers
according to FAKE_CLAUDE_MODE.

What matters most here is what a review would not catch: that a keyless caller
never reaches the CLI, that the CLI runs with every tool off, and that no
`claude` process outlives the slot it was holding.
"""
import asyncio
import json
import os
import sys
import textwrap

import httpx
import pytest
from fastapi.testclient import TestClient

from app import claude_cli, clientinfo, registry, slots, upstream
from app import config as conf
from test_proxy import completion, events, mock_response  # noqa: F401 - events is a fixture

FAKE = textwrap.dedent('''
    import json, os, sys, time
    argv = sys.argv[1:]
    with open(argv[argv.index("--system-prompt-file") + 1], encoding="utf-8") as f:
        system = f.read()
    seen = {"argv": argv, "system": system, "prompt": sys.stdin.read(), "cwd": os.getcwd()}
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(seen) + "\\n")
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    if mode == "hang":
        with open(os.environ["FAKE_CLAUDE_PID"], "w") as f:
            f.write(str(os.getpid()))
        time.sleep(60)
    if mode == "garbage":
        sys.stderr.write("something went very wrong\\n")
        sys.exit(3)
    # A model calling functions as tools, the way `--output-format stream-json`
    # prints it: block by block, then the CLI's "no such tool" reply. The real CLI
    # would go on from there; this one sleeps, so a run that is not cut short hangs.
    calls = json.loads(os.environ.get("FAKE_CLAUDE_CALLS") or "[]")
    if calls:
        message = {"id": "msg_1", "model": "claude-opus-test-1", "usage": {"input_tokens": 7, "output_tokens": 3}}
        def emit(event):
            print(json.dumps(event), flush=True)
        emit({"type": "system", "subtype": "init", "tools": ["StructuredOutput"]})
        emit({"type": "assistant", "message": {**message, "content": [{"type": "text", "text": "Checking."}]}})
        for i, (name, args) in enumerate(calls):
            block = {"type": "tool_use", "id": "toolu_%d" % i, "name": name, "input": args}
            emit({"type": "assistant", "message": {**message, "content": [block]}})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "No such tool"}]}})
        with open(os.environ["FAKE_CLAUDE_PID"], "w") as f:
            f.write(str(os.getpid()))
        if mode != "carry-on":
            time.sleep(60)
    result = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "echo:" + seen["prompt"], "session_id": "s1", "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 3,
                  "cache_read_input_tokens": 2, "output_tokens": 4},
        "modelUsage": {"claude-opus-test-1": {"outputTokens": 4}},
    }
    # What --json-schema makes the real CLI print: the reply both as text and parsed.
    structured = os.environ.get("FAKE_CLAUDE_STRUCTURED")
    if structured:
        result.update(result=structured, structured_output=json.loads(structured), stop_reason="tool_use")
    if mode.startswith("error"):
        status = int(mode[5:]) if mode[5:] else None
        result.update(is_error=True, api_error_status=status, result="it broke", usage={})
    # The real CLI can print a diagnostic line ahead of its result.
    print('[claude-code:diagnostic] {"noise": true}')
    print(json.dumps(result))
    sys.exit(1 if result["is_error"] else 0)
''')

CONFIG = """\
models:
  claude-opus:
    targets:
      - {provider: claude, model: opus, priority: 1}
      - {provider: backup, priority: 2}

providers:
  - name: claude
    kind: claude-cli
    slots: 1
    require_permission: true
    enabled_models: [opus, sonnet]
    model_map:
      sonnet: claude-sonnet

  - name: backup
    base_url: "http://backup.invalid/v1"
    slots: 1
    enabled_models: ["vendor/opus-elsewhere"]
    model_map:
      "vendor/opus-elsewhere": claude-opus

auth:
  keys: ["test-key"]

routing:
  # See test_proxy.py: a leaked single slot must fail the suite, not hang it.
  queue_timeout: 3
  failover: true
  down_backoff: 15
"""

AUTH = {"Authorization": "Bearer test-key"}


def chat(model="claude-opus", **extra):
    body = {"model": model, "messages": [{"role": "user", "content": "hello"}]}
    body.update(extra)
    return body


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def cli(load_config, monkeypatch, tmp_path):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setattr(claude_cli, "COMMAND", [sys.executable, str(script)])
    monkeypatch.setattr(claude_cli, "_client", None)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_PID", str(tmp_path / "pid"))
    monkeypatch.setattr(conf, "RESOLVE_CLIENT_HOST", False)
    monkeypatch.setattr(clientinfo, "_dns_cache", {})
    load_config(CONFIG)

    backup = []

    def handler(request):
        backup.append(request)
        return mock_response(200, json=completion(text="from backup"))

    forward = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(upstream, "forward_client", lambda: forward)

    class Harness:
        backup_requests = backup
        pid_file = tmp_path / "pid"

        @staticmethod
        def calls():
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return Harness()


@pytest.fixture
def client(cli):
    from app import main
    with TestClient(main.app) as c:
        yield c


def idle():
    return {name: slots.in_use(name) for name in ("claude", "backup")}


# ── Happy path ──────────────────────────────────────────────────────────────

def test_a_chat_completion_runs_the_cli(client, cli):
    r = client.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "echo:hello"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["model"] == "claude-opus-test-1"
    # Cached prompt tokens count as prompt tokens.
    assert body["usage"] == {"prompt_tokens": 15, "completion_tokens": 4, "total_tokens": 19}
    (call,) = cli.calls()
    assert call["argv"][call["argv"].index("--model") + 1] == "opus"
    assert call["prompt"] == "hello"
    assert call["system"] == claude_cli.DEFAULT_SYSTEM
    assert idle() == {"claude": 0, "backup": 0}


@pytest.mark.parametrize("stream", [False, True])
def test_the_log_names_the_concrete_model_behind_the_alias(client, cli, events, stream):
    client.post("/v1/chat/completions", json=chat("claude-sonnet", stream=stream), headers=AUTH)
    line = next(l for l in events() if "event=request" in l)
    assert "model=sonnet " in line and "asked=claude-sonnet" in line
    assert "served=claude-opus-test-1" in line


def test_the_cli_runs_with_every_tool_off_in_an_empty_directory(client, cli):
    client.post("/v1/chat/completions", json=chat(), headers=AUTH)
    call = cli.calls()[0]
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    for flag in ("-p", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert "--effort" not in argv
    assert os.path.basename(call["cwd"]).startswith("llm-proxy-claude-")
    # Only the per-request system prompt file was ever in it, and it is gone.
    assert os.listdir(call["cwd"]) == []


def test_reasoning_effort_becomes_the_effort_flag(client, cli):
    client.post("/v1/chat/completions", json=chat("claude-sonnet", reasoning_effort="high"), headers=AUTH)
    argv = cli.calls()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "high"


def test_history_becomes_a_transcript_and_system_the_system_prompt(client, cli):
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello!"},
        {"role": "system", "content": "[note]"},
        {"role": "user", "content": [{"type": "text", "text": "how"}, {"type": "text", "text": "are you"}]},
    ]
    r = client.post("/v1/chat/completions", json={"model": "claude-opus", "messages": messages}, headers=AUTH)
    assert r.status_code == 200
    call = cli.calls()[0]
    assert call["system"] == "be brief"
    assert (
        "<user>\nhi\n</user>\n<assistant>\nhello!\n</assistant>\n"
        "<system>\n[note]\n</system>\n<user>\nhow\nare you\n</user>"
    ) in call["prompt"]


def test_a_stream_request_gets_the_answer_as_sse(client, cli):
    with client.stream("POST", "/v1/chat/completions", json=chat(stream=True), headers=AUTH) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    assert data[-1] == "[DONE]"
    chunks = [json.loads(d) for d in data[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": "echo:hello"}
    assert chunks[-1]["usage"]["completion_tokens"] == 4
    row = client.get("/admin/inflight", headers=AUTH).json()["requests"][0]
    assert row["provider"] == "claude" and row["out_tokens"] == 4
    assert idle() == {"claude": 0, "backup": 0}


# ── The gate ────────────────────────────────────────────────────────────────

def test_a_keyless_caller_never_reaches_the_cli(client, cli):
    r = client.post("/v1/chat/completions", json=chat())
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "from backup"
    assert client.post("/v1/chat/completions", json=chat("claude-sonnet")).status_code == 401
    listed = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert "claude-sonnet" not in listed
    assert cli.calls() == []


def test_a_model_less_passthrough_never_picks_the_cli(client, cli):
    r = client.post("/v1/audio/transcriptions", content=b"raw bytes", headers=AUTH)
    assert r.status_code == 200
    assert len(cli.backup_requests) == 1
    assert cli.calls() == []


# ── Errors and failover ─────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,status", [
    ("error429", 429),   # usage limit: relayed, and a failover trigger
    ("error404", 404),   # unknown model
    ("error529", 503),   # Anthropic's "overloaded" -> what failover understands
    ("error401", 502),   # the CLI's own login failed: not the caller's key
    ("error", 502),      # no API status at all (e.g. not logged in)
])
def test_cli_errors_become_http_statuses(client, cli, monkeypatch, mode, status):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    r = client.post("/v1/chat/completions", json=chat("claude-sonnet"), headers=AUTH)
    assert r.status_code == status
    assert r.json()["error"]["message"] == "it broke"
    assert idle() == {"claude": 0, "backup": 0}


def test_a_cli_failure_fails_over_to_the_next_target(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error429")
    r = client.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "from backup"
    assert registry.is_down("claude")
    assert idle() == {"claude": 0, "backup": 0}


def test_output_without_a_result_is_a_502_that_carries_stderr(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    r = client.post("/v1/chat/completions", json=chat("claude-sonnet"), headers=AUTH)
    assert r.status_code == 502
    message = r.json()["error"]["message"]
    assert "exited 3" in message and "something went very wrong" in message


def test_a_missing_binary_is_a_connection_failure(client, cli, monkeypatch):
    monkeypatch.setattr(claude_cli, "COMMAND", ["/nonexistent/claude"])
    r = client.post("/v1/chat/completions", json=chat("claude-sonnet"), headers=AUTH)
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "upstream_unavailable"
    # ...and like any unreachable backend, a logical model moves on.
    registry._down_until.clear()
    r = client.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert r.json()["choices"][0]["message"]["content"] == "from backup"
    assert idle() == {"claude": 0, "backup": 0}


@pytest.mark.parametrize("extra", [
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:,"}}]}]},
    {"functions": [{"name": "f", "parameters": {}}]},
    {"messages": [{"role": "user", "content": "x"}, {"role": "function", "name": "f", "content": "r"}]},
    {"tools": [{"type": "web_search"}]},
    {"tools": [{"type": "function", "function": {"name": "f"}}] * 2},
    {"tools": [{"type": "function", "function": {"name": "f"}}],
     "tool_choice": {"type": "function", "function": {"name": "g"}}},
    {"messages": [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function"}]},
    ]},
])
def test_requests_it_cannot_serve_are_refused_not_degraded(client, cli, extra):
    r = client.post("/v1/chat/completions", json=chat("claude-sonnet", **extra), headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert cli.calls() == []


# ── Function calling ────────────────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    }},
    {"type": "function", "function": {"name": "search_web", "parameters": {"type": "object"}}},
]

WEATHER = [["get_weather", {"city": "Curitiba"}]]


def ask(client, **extra):
    return client.post("/v1/chat/completions", json=chat("claude-sonnet", tools=TOOLS, **extra), headers=AUTH)


def test_functions_reach_the_model_as_text_never_as_tools(client, cli):
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "weather?"}]
    assert ask(client, messages=messages).status_code == 200
    call = cli.calls()[0]
    argv = call["argv"]
    # The CLI's own tool list stays empty; the functions are prompt text.
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert argv[argv.index("--json-schema") + 1] == claude_cli._REPLY
    assert call["system"].startswith("be brief\n\n# Functions\n")
    assert '{"name": "get_weather", "description": "Current weather for a city.", "parameters": ' in call["system"]
    assert call["prompt"] == "weather?"


def test_a_call_is_caught_and_goes_back_as_tool_calls(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", json.dumps(WEATHER))
    r = ask(client)
    assert r.status_code == 200
    body = r.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Checking."
    (call,) = choice["message"]["tool_calls"]
    assert call["id"].startswith("call_") and call["type"] == "function"
    assert call["function"] == {"name": "get_weather", "arguments": '{"city": "Curitiba"}'}
    assert body["model"] == "claude-opus-test-1"
    assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    # Cut short the moment the call was complete: the CLI never got to carry on.
    assert not alive(int(cli.pid_file.read_text()))
    assert idle() == {"claude": 0, "backup": 0}


def test_calls_made_together_go_back_together(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", json.dumps(WEATHER + [["search_web", {"query": "rain"}]]))
    calls = ask(client).json()["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["get_weather", "search_web"]
    assert calls[0]["id"] != calls[1]["id"]


def test_parallel_tool_calls_false_sends_back_one(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", json.dumps(WEATHER + [["search_web", {"query": "rain"}]]))
    calls = ask(client, parallel_tool_calls=False).json()["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["get_weather"]
    assert "Call one function at a time." in cli.calls()[0]["system"]


def test_a_forced_tool_choice_catches_only_that_function(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", json.dumps([["search_web", {}], ["get_weather", {"city": "Rio"}]]))
    forced = {"type": "function", "function": {"name": "get_weather"}}
    (call,) = ask(client, tool_choice=forced).json()["choices"][0]["message"]["tool_calls"]
    assert call["function"] == {"name": "get_weather", "arguments": '{"city": "Rio"}'}
    assert "This turn, call `get_weather`." in cli.calls()[0]["system"]


def test_a_call_to_a_function_not_offered_is_left_to_the_cli(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", json.dumps([["rm_rf", {}]]))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "carry-on")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"content": "I can't do that."}))
    choice = ask(client).json()["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "I can't do that."}
    assert choice["finish_reason"] == "stop"


def test_a_streamed_call_arrives_as_a_tool_calls_delta(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", json.dumps(WEATHER))
    body = chat("claude-sonnet", tools=TOOLS, stream=True)
    with client.stream("POST", "/v1/chat/completions", json=body, headers=AUTH) as r:
        text = "".join(r.iter_text())
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    chunks = [json.loads(d) for d in data[:-1]]
    delta = chunks[0]["choices"][0]["delta"]
    assert delta["content"] == "Checking."
    (call,) = delta["tool_calls"]
    assert call["index"] == 0 and call["function"]["name"] == "get_weather"
    assert chunks[1]["choices"][0]["finish_reason"] == "tool_calls"
    assert idle() == {"claude": 0, "backup": 0}


def test_an_answer_is_the_content_of_the_structured_reply(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"content": "Sunny."}))
    choice = ask(client).json()["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "Sunny."}
    assert choice["finish_reason"] == "stop"


def test_an_answer_without_the_structured_reply_is_still_an_answer(client, cli):
    assert ask(client).json()["choices"][0]["message"] == {"role": "assistant", "content": "echo:hello"}


def test_calls_and_results_in_the_history_become_part_of_the_transcript(client, cli):
    messages = [
        {"role": "user", "content": "weather in Curitiba?"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Curitiba"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "18C, rain"},
    ]
    body = chat("claude-sonnet", tools=TOOLS, messages=messages)
    assert client.post("/v1/chat/completions", json=body, headers=AUTH).status_code == 200
    prompt = cli.calls()[0]["prompt"]
    assert (
        '<assistant>\n<function_call id="call_1" name="get_weather">{"city": "Curitiba"}</function_call>\n'
        '</assistant>\n<function_result id="call_1" name="get_weather">\n18C, rain\n</function_result>'
    ) in prompt
    assert "call the functions it needs" in prompt


def test_tool_choice_required_asks_for_a_call(client, cli):
    assert ask(client, tool_choice="required").status_code == 200
    assert "This turn, call at least one function." in cli.calls()[0]["system"]


def test_tool_choice_none_is_a_plain_chat(client, cli):
    assert ask(client, tool_choice="none").status_code == 200
    call = cli.calls()[0]
    assert call["argv"][call["argv"].index("--output-format") + 1] == "json"
    assert "--json-schema" not in call["argv"]
    assert "# Functions" not in call["system"]


# ── No process outlives its slot ────────────────────────────────────────────

def test_a_timeout_kills_the_process(client, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    monkeypatch.setattr(claude_cli, "_client", httpx.AsyncClient(
        transport=claude_cli.Transport(), base_url="http://claude-cli", timeout=httpx.Timeout(1.0)
    ))
    r = client.post("/v1/chat/completions", json=chat("claude-sonnet"), headers=AUTH)
    assert r.status_code == 504
    assert not alive(int(cli.pid_file.read_text()))
    assert idle() == {"claude": 0, "backup": 0}


async def test_cancellation_kills_the_process(cli, monkeypatch, tmp_path):
    """The Kill button cancels the task awaiting the CLI."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    system = tmp_path / "system.txt"
    system.write_text("s", encoding="utf-8")
    request = httpx.Request("POST", "http://claude-cli/v1/chat/completions")
    task = asyncio.create_task(
        claude_cli._run(claude_cli._argv("opus", str(system), {}), "hi", None, request)
    )
    for _ in range(500):
        if cli.pid_file.exists() and cli.pid_file.read_text():
            break
        await asyncio.sleep(0.01)
    pid = int(cli.pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(pid)


# ── Config ──────────────────────────────────────────────────────────────────

async def test_a_cli_provider_needs_no_base_url_and_lists_its_aliases(load_config):
    load_config("""\
        providers:
          - name: claude
            kind: claude-cli
        """)
    p = conf.PROVIDERS_BY_NAME["claude"]
    assert p.kind == conf.KIND_CLAUDE_CLI and p.base_url == ""
    assert p.lists_all
    assert await registry._fetch_live(p) == list(conf.CLAUDE_CLI_MODELS)


def test_an_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown kind"):
        conf._load(b"providers:\n  - name: x\n    kind: grpc\n")


def test_an_http_provider_still_requires_a_base_url():
    with pytest.raises(KeyError, match="base_url"):
        conf._load(b"providers:\n  - name: x\n")
