"""Form uploads (`multipart/form-data`) routed by their `model` field.

A transcription arrives as a form, not JSON, so before this the proxy saw no
model and passed it through to the first backend the caller may use, which was
never the one serving whisper. These drive the real ASGI app with a mock
upstream, as test_proxy.py does, and check the four things that matter: the
form reaches the backend that serves the model, it arrives byte for byte (only
the `model` field ever changes), the auth gate and the 404 behave as for JSON,
and the audio never lands in the In-flight body store or the log.
"""
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app import clientinfo, inflight, multipart, registry, slots, trim, upstream
from app import config as conf
from test_proxy import mock_response

CONFIG = """\
providers:
  # Gated and first, so an authorized passthrough would land here.
  - name: paid
    base_url: "http://paid.invalid"
    require_permission: true
    enabled_models: ["vendor/gpt-transcribe"]
    model_map:
      "vendor/gpt-transcribe": transcribe-paid

  # The first open backend: where a keyless passthrough lands. No whisper here.
  - name: chat
    base_url: "http://chat.invalid"
    enabled_models: ["chat-model"]

  - name: whisper
    base_url: "http://whisper.invalid"
    slots: 1
    enabled_models: ["whisper", "ggml-large-v3"]
    model_map:
      "ggml-large-v3": whisper-large

  - name: backup
    base_url: "http://backup.invalid"
    slots: 1
    enabled_models: ["whisper"]

routing:
  queue_timeout: 3
  failover: true
  down_backoff: 15
  failover_statuses: [429, 500, 502, 503, 504]

auth:
  keys: ["test-key"]
"""

AUTH = {"Authorization": "Bearer test-key"}

# Binary that looks like the framing on purpose: CRLFs, dashes, a fake
# delimiter line and invalid UTF-8. Only a parser that respects the boundary
# leaves it alone.
AUDIO = b"RIFF\x00\x01\xff\xfe\r\n--not-the-boundary\r\n\r\n" + bytes(range(256)) * 64
SECRET = b"RIFF\x00\x01\xff\xfe"


def form(model="whisper", boundary="b0undary", extra=(), audio=AUDIO):
    """A form body as curl -F builds it, plus its Content-Type."""
    parts = []
    if model is not None:
        parts.append(f'Content-Disposition: form-data; name="model"\r\n\r\n{model}'.encode())
    for name, value in extra:
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}'.encode())
    parts.append(
        b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
        b"Content-Type: audio/wav\r\n\r\n" + audio
    )
    delim = b"--" + boundary.encode()
    body = b"".join(delim + b"\r\n" + p + b"\r\n" for p in parts) + delim + b"--\r\n"
    return body, f"multipart/form-data; boundary={boundary}"


def transcript(text="hello"):
    return {"text": text}


@pytest.fixture
def seen(load_config, monkeypatch):
    async def fake_cached_live(provider):
        return list(provider.enabled_models)

    monkeypatch.setattr(registry, "_cached_live", fake_cached_live)
    monkeypatch.setattr(conf, "RESOLVE_CLIENT_HOST", False)
    monkeypatch.setattr(clientinfo, "_dns_cache", {})
    load_config(CONFIG)

    class Seen(list):
        def set(self, fn):
            handler["fn"] = fn

    requests = Seen()
    handler = {"fn": lambda r: mock_response(200, json=transcript())}

    def transport(request):
        requests.append(request)
        return handler["fn"](request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(upstream, "forward_client", lambda: client)
    return requests


@pytest.fixture
def client(seen):
    from app import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def events(caplog):
    logger = logging.getLogger("llm-proxy.event")
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="llm-proxy.event")
    yield lambda: [r.getMessage() for r in caplog.records if r.name == "llm-proxy.event"]
    logger.propagate = False


def post(client, body, ctype, path="/v1/audio/transcriptions", headers=None):
    return client.post(path, content=body, headers={"content-type": ctype, **(headers or {})})


def idle():
    return {name: slots.in_use(name) for name in ("whisper", "backup")}


# ── Routing ─────────────────────────────────────────────────────────────────

def test_a_form_goes_to_the_backend_that_serves_its_model(client, seen):
    body, ctype = form()
    r = post(client, body, ctype)
    assert r.status_code == 200, r.text
    assert r.json() == transcript()
    assert [q.url.host for q in seen] == ["whisper.invalid"]
    assert seen[0].url.path == "/v1/audio/transcriptions"
    assert idle() == {"whisper": 0, "backup": 0}


def test_the_backend_receives_the_form_byte_for_byte(client, seen):
    body, ctype = form(extra=[("response_format", "verbose_json")])
    post(client, body, ctype)
    assert seen[0].content == body
    assert seen[0].headers["content-type"] == ctype
    assert seen[0].headers["content-length"] == str(len(body))


def test_only_the_model_field_is_rewritten_to_the_native_id(client, seen):
    body, ctype = form(model="whisper-large", extra=[("language", "pt")])
    post(client, body, ctype)
    expected, _ = form(model="ggml-large-v3", extra=[("language", "pt")])
    assert seen[0].content == expected
    assert seen[0].headers["content-type"] == ctype


def test_the_same_rule_holds_on_any_path(client, seen):
    body, ctype = form()
    post(client, body, ctype, path="/v1/audio/translations")
    assert [q.url.host for q in seen] == ["whisper.invalid"]
    assert seen[0].url.path == "/v1/audio/translations"


def test_a_form_without_a_model_is_still_a_passthrough(client, seen, events):
    body, ctype = form(model=None)
    r = post(client, body, ctype)
    assert r.status_code == 200
    assert [q.url.host for q in seen] == ["chat.invalid"]
    assert seen[0].content == body
    assert any("event=passthrough" in line for line in events())


def test_a_body_that_is_not_the_form_it_claims_is_a_passthrough(client, seen):
    r = post(client, b"no boundary in here", "multipart/form-data; boundary=xyz")
    assert r.status_code == 200
    assert [q.url.host for q in seen] == ["chat.invalid"]


# ── Gate and not-found ──────────────────────────────────────────────────────

def test_a_gated_model_without_a_key_is_401(client, seen):
    body, ctype = form(model="transcribe-paid")
    r = post(client, body, ctype)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["type"] == "unauthorized"
    assert seen == []


def test_a_gated_model_with_a_key_is_forwarded(client, seen):
    body, ctype = form(model="transcribe-paid")
    r = post(client, body, ctype, headers=AUTH)
    assert r.status_code == 200, r.text
    assert [q.url.host for q in seen] == ["paid.invalid"]
    expected, _ = form(model="vendor/gpt-transcribe")
    assert seen[0].content == expected


def test_an_unknown_model_is_404_model_not_found(client, seen):
    body, ctype = form(model="nobody-serves-this")
    r = post(client, body, ctype, headers=AUTH)
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "model_not_found" and err["type"] == "invalid_request_error"
    assert seen == []


# ── Failover, slots, guardrail ──────────────────────────────────────────────

def test_a_failing_backend_fails_over_with_the_same_body(client, seen):
    seen.set(lambda r: mock_response(503, json={"error": "busy"})
             if r.url.host == "whisper.invalid" else mock_response(200, json=transcript("ok")))
    body, ctype = form()
    r = post(client, body, ctype)
    assert r.status_code == 200 and r.json() == transcript("ok")
    assert [q.url.host for q in seen] == ["whisper.invalid", "backup.invalid"]
    assert seen[1].content == body
    assert idle() == {"whisper": 0, "backup": 0}


def test_an_unreachable_backend_fails_over(client, seen):
    def handler(r):
        if r.url.host == "whisper.invalid":
            raise httpx.ConnectError("refused", request=r)
        return mock_response(200, json=transcript())
    seen.set(handler)
    body, ctype = form()
    assert post(client, body, ctype).status_code == 200
    assert [q.url.host for q in seen] == ["whisper.invalid", "backup.invalid"]
    assert idle() == {"whisper": 0, "backup": 0}


def test_a_form_takes_a_slot_like_any_request(client, seen):
    held = {}

    def handler(r):
        held[r.url.host] = slots.in_use(r.url.host.split(".")[0])
        return mock_response(200, json=transcript())
    seen.set(handler)
    body, ctype = form()
    post(client, body, ctype)
    assert held == {"whisper.invalid": 1}
    assert idle() == {"whisper": 0, "backup": 0}


def test_the_context_guardrail_never_sees_a_form(client, seen, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("trim_request ran on a form upload")
    monkeypatch.setattr(trim, "trim_request", boom)
    body, ctype = form(extra=[("num_ctx", "10")])
    assert post(client, body, ctype).status_code == 200


# ── What is kept and logged ─────────────────────────────────────────────────

def test_the_audio_is_not_kept_for_the_in_flight_view(client, seen):
    body, ctype = form()
    post(client, body, ctype)
    entry_id = max(inflight._bodies)
    request = inflight.bodies(entry_id)["request"]
    assert "a.wav" in request and "audio/wav" in request and "model=whisper" in request
    assert str(len(body)) in request
    assert "RIFF" not in request and "not-the-boundary" not in request
    assert len(request) < 500


def test_the_audio_never_reaches_the_log_even_with_input_logging(client, seen, monkeypatch, caplog):
    monkeypatch.setattr(conf, "LOG_INPUT", True)
    caplog.set_level(logging.INFO)
    body, ctype = form()
    post(client, body, ctype)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "a.wav" in text, "the summary is what LOG_INPUT shows"
    assert "RIFF" not in text and "not-the-boundary" not in text


def test_the_request_line_is_tagged_and_carries_no_throughput(client, seen, events):
    body, ctype = form()
    post(client, body, ctype)
    line = next(l for l in events() if "event=request" in l)
    assert "op=transcription" in line and "model=whisper" in line and "status=200" in line
    assert "speed_tps=" not in line and "in_tps=" not in line
    assert "cost=" not in line

    costs = client.get("/admin/costs", headers=AUTH).json()
    assert costs["totals"]["requests"] == 1 and costs["totals"]["priced"] == 0
    row = client.get("/admin/inflight", headers=AUTH).json()["requests"][0]
    assert row["op"] == "transcription" and row["tps"] is None


def test_a_stream_field_streams(client, seen):
    sse = b'data: {"type":"transcript.text.delta","delta":"hi"}\n\ndata: [DONE]\n\n'
    seen.set(lambda r: mock_response(200, content=sse, headers={"content-type": "text/event-stream"}))
    body, ctype = form(extra=[("stream", "true")])
    r = post(client, body, ctype)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.content == sse
    assert seen[0].content == body
    assert idle() == {"whisper": 0, "backup": 0}


# ── The parser ──────────────────────────────────────────────────────────────

def test_parse_reads_quoted_boundaries_and_skips_a_preamble():
    body, _ = form(boundary="a b")
    parsed = multipart.parse(b"preamble\r\n" + body, 'multipart/form-data; boundary="a b"')
    assert parsed.value("model") == "whisper"
    assert [p.name for p in parsed.parts] == ["model", "file"]
    f = parsed.parts[1]
    assert (f.filename, f.content_type) == ("a.wav", "audio/wav")
    assert parsed.body[f.start:f.end] == AUDIO


def test_parse_ignores_what_is_not_a_form():
    body, ctype = form()
    assert multipart.parse(body, "application/json") is None
    assert multipart.parse(body, None) is None
    assert multipart.parse(body, "multipart/form-data") is None  # no boundary
    assert multipart.parse(body[:-30], ctype) is None  # truncated


def test_with_model_returns_the_original_bytes_when_nothing_changes():
    body, ctype = form()
    parsed = multipart.parse(body, ctype)
    assert parsed.with_model("whisper") is body


def test_a_file_named_model_is_never_read_or_rewritten():
    delim = b"--x"
    body = (delim + b'\r\nContent-Disposition: form-data; name="model"; filename="m.bin"\r\n\r\nraw'
            + b"\r\n" + delim + b"--\r\n")
    parsed = multipart.parse(body, "multipart/form-data; boundary=x")
    assert parsed.value("model") is None
    assert parsed.with_model("other") is body
