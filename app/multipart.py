"""Just enough of `multipart/form-data` to route an upload by its `model` field.

Why this exists. The OpenAI audio endpoints (`/v1/audio/transcriptions`,
`/v1/audio/translations`) and a few others take a form upload, not JSON, and name
the model in a form field. Without reading that field the proxy saw a request
with no model and passed it through to the first backend the caller may use,
which is almost never the one that serves whisper. The decision is made on the
Content-Type, never the path, so any OpenAI-style form route is covered.

What it does, and what it deliberately does not:

- `parse` finds where each part's value sits in the body and nothing else. It
  never copies or decodes a file part: an audio upload is megabytes of binary,
  and the only things the proxy needs from it are its size, its name and its
  type, for the In-flight row and the log (`Form.summary`).
- `Form.with_model` is the one rewrite: the value of the `model` part, replaced
  by the backend's native id when its `model_map` differs. Every other byte of
  the body, the boundary and the other parts included, is the client's own, so
  the Content-Type header forwarded unchanged still describes it.
- A body that does not parse as the form its header claims returns None and is
  treated like any other body the proxy cannot read: passed through untouched.

Stdlib only. Imports nothing from the app.
"""
from dataclasses import dataclass
from email.message import Message
from typing import List, Optional

CRLF = b"\r\n"

# A text field longer than this is cut in the summary. The summary is what the
# In-flight row and LOG_INPUT show for the request, so it must stay small.
_SUMMARY_TEXT = 200


@dataclass
class Part:
    name: Optional[str]
    filename: Optional[str]
    content_type: Optional[str]
    start: int  # value's first byte in the body
    end: int    # one past its last byte

    @property
    def is_file(self) -> bool:
        return self.filename is not None


def _header_param(header: str, value: str, param: str) -> Optional[str]:
    """A parameter of a MIME header (`boundary`, `name`, `filename`), quoted or
    not, read the way the email package reads it."""
    m = Message()
    m[header] = value
    got = m.get_param(param, header=header)
    if got is None:
        return None
    if isinstance(got, tuple):  # RFC 2231 encoded
        from email.utils import collapse_rfc2231_value
        return collapse_rfc2231_value(got)
    return got


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _parse_headers(raw: bytes):
    """name, filename and content type from one part's header block."""
    name = filename = ctype = None
    for line in raw.split(CRLF):
        key, sep, value = line.decode("utf-8", errors="replace").partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "content-disposition":
            name = _header_param(key, value, "name")
            filename = _header_param(key, value, "filename")
        elif key == "content-type":
            ctype = value
    return name, filename, ctype


class Form:
    """A parsed form: the client's body, kept whole, and where each part sits."""

    def __init__(self, body: bytes, content_type: str, parts: List[Part]):
        self.body = body
        self.content_type = content_type
        self.parts = parts

    def value(self, name: str) -> Optional[str]:
        """The text of the first non-file field called `name`, stripped, or None."""
        for p in self.parts:
            if p.name == name and not p.is_file:
                return self.body[p.start:p.end].decode("utf-8", errors="replace").strip()
        return None

    def with_model(self, model: str) -> bytes:
        """The body with every `model` field's value replaced by `model`.

        The original bytes object is returned when nothing changes, so a backend
        whose native id is the name the client sent gets the body byte for byte.
        Every `model` field is rewritten, not just the first: a backend that reads
        the last one must not see the client's name either.
        """
        new = model.encode("utf-8")
        spans = [
            (p.start, p.end) for p in self.parts
            if p.name == "model" and not p.is_file and self.body[p.start:p.end] != new
        ]
        if not spans:
            return self.body
        out, pos = [], 0
        for start, end in spans:
            out += [self.body[pos:start], new]
            pos = end
        out.append(self.body[pos:])
        return b"".join(out)

    def summary(self, model: Optional[str] = None) -> str:
        """What the request was, without its payload: media type, size, and each
        field. A file is its name, type and size; a text field its value, cut at
        `_SUMMARY_TEXT`; a non-file part that is not text, its size. `model`, when
        given, stands in for the `model` field (the native id actually sent)."""
        fields = []
        for p in self.parts:
            size = p.end - p.start
            if p.is_file:
                kind = p.content_type or "application/octet-stream"
                fields.append(f"{p.name}=<file {p.filename!r}, {kind}, {size} bytes>")
            elif p.content_type and not _media_type(p.content_type).startswith("text/"):
                fields.append(f"{p.name}=<{p.content_type}, {size} bytes>")
            else:
                text = model if (p.name == "model" and model) else (
                    self.body[p.start:p.end].decode("utf-8", errors="replace")
                )
                if len(text) > _SUMMARY_TEXT:
                    text = text[:_SUMMARY_TEXT] + "…"
                fields.append(f"{p.name}={text}")
        head = f"[multipart/form-data, {len(self.body)} bytes]"
        return head + ("\n" + "\n".join(fields) if fields else "")


def is_form(content_type: Optional[str]) -> bool:
    return bool(content_type) and _media_type(content_type) == "multipart/form-data"


def parse(body: bytes, content_type: Optional[str]) -> Optional[Form]:
    """The form in `body`, or None when the Content-Type is not
    `multipart/form-data` or the body is not the form it claims to be."""
    if not is_form(content_type):
        return None
    boundary = _header_param("content-type", content_type, "boundary")
    if not boundary:
        return None
    delim = b"--" + boundary.encode("latin-1", errors="replace")

    # The first delimiter starts the body or follows a preamble's CRLF.
    if body.startswith(delim):
        pos = len(delim)
    else:
        found = body.find(CRLF + delim)
        if found < 0:
            return None
        pos = found + 2 + len(delim)

    parts: List[Part] = []
    sep = CRLF + delim
    while True:
        if body[pos:pos + 2] == b"--":
            return Form(body, content_type, parts)  # closing delimiter
        eol = body.find(CRLF, pos)  # skip transport padding to the line's end
        if eol < 0:
            return None
        start = eol + 2
        end = body.find(sep, start)
        if end < 0:
            return None
        if body[start:start + 2] == CRLF:  # a part with no headers
            head, value_start = b"", start + 2
        else:
            hend = body.find(CRLF + CRLF, start, end)
            if hend < 0:
                return None
            head, value_start = body[start:hend], hend + 4
        name, filename, ctype = _parse_headers(head)
        parts.append(Part(name, filename, ctype, value_start, end))
        pos = end + len(sep)
