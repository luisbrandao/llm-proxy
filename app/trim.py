"""Context-window guardrail: shrink a chat request that cannot fit the context
the client itself declared.

Why this exists. A chat client (Open WebUI here) sends the *whole* conversation
on every turn, and tags the request with the model's context size as `num_ctx`.
Its own history-trimming filter failed one night, and a 512 KB conversation went
to a paid backend verbatim: a large bill for a reply the model could not even
ground in a context that size. The proxy is the last hop every request passes
through, so this is where the guardrail lives.

What it does. Only for a JSON chat body that has a `messages` list **and** an
integer `num_ctx`, and only when the request is counted to exceed
`num_ctx - response_headroom`:

1. Cap oversized *old* tool results (`role: tool`, deeper than `protect_recent`
   messages from the end, larger than `max_tool_result_tokens`) to a head+tail
   excerpt with a marker. One 20 KB page dump buried in history should not
   force every older turn out.
2. If still over budget, drop the oldest turns until the rest fits. System
   messages are always kept. An assistant message that requests tools and the
   `tool` results answering it are one atomic block, so the window never
   starts on a dangling tool call (which most backends reject with a 400).
3. The newest block is always kept, even if it alone is over budget: there is
   nothing smaller we can send, and the backend's own error is the right answer.

A request that fits is passed through untouched — not a byte changes. Nothing
here runs for bodies without `num_ctx`, so a client that manages its own
context is never second-guessed.

Token count. With `trim.tokenizer` naming a Hugging Face `tokenizer.json`
(the image bakes in Qwen's — every local model here is Qwen3.5 or later, which
share one vocabulary) each message is counted the way the chat template prints
it: the text of every content part, reasoning field and tool-call argument is
tokenized as-is, not as its JSON-escaped serialization, plus a small fixed cost
per message, call and argument for the template's own markup, plus the tool
schemas the template renders into the system prompt. Against Qwen3.8's real
template on agent transcripts that lands within ~1%, and errs high (hundreds of
one-line turns: +9%), never meaningfully low. Sampling fields are not counted:
they never reach the model as tokens.

Without a tokenizer (unset, missing file, library absent) it falls back to the
old estimate, the serialized JSON length divided by `chars_per_token`. That one
is wrong in both directions on agent traffic, which is why it was replaced: 1.45×
over on source code (escaped quotes and newlines), 0.8× *under* on an `ls -l`
listing, because Qwen spends a token on every digit. Either way, non-text content
parts (images) count a flat `MEDIA_PART_TOKENS` each rather than their base64
length, which would otherwise dwarf everything.
"""
import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Say so explicitly, or `tokenizers` disables its thread pool in every child the
# process forks after using it, and prints a warning there. The claude-cli
# backend forks the `claude` binary and reads its stderr as the error message.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "true")

from app import config as conf
from app.metrics import TRIMS_TOTAL

logger = logging.getLogger("llm-proxy")

# Flat cost of one non-text content part (image_url, input_audio, …). Roughly
# an OpenAI high-detail image; its base64 payload is not text and must not be
# measured as such.
MEDIA_PART_TOKENS = 1000

TRUNCATION_MARK = "\n\n…[llm-proxy cut {n} characters from the middle of this tool result]…\n\n"

# The chat template's own markup, in tokens, measured on Qwen3.8's template and
# rounded up: `<|im_start|>role\n…<|im_end|>\n` (+ `<think>` on an assistant turn)
# per message; `<tool_call>\n<function=…>\n…</function>\n</tool_call>` per call;
# `<parameter=…>\n…\n</parameter>\n` per argument; the tools preamble that
# precedes the schemas; and the generation prompt plus reasoning-effort line.
MSG_MARKUP_TOKENS = 8
CALL_MARKUP_TOKENS = 12
ARG_MARKUP_TOKENS = 10
TOOLS_PREAMBLE_TOKENS = 320
PROMPT_MARKUP_TOKENS = 64


@dataclass
class Trimmed:
    """What a trim did — the payload to forward plus the numbers the request
    log and the In-flight row surface, so the same figures appear in both."""
    payload: dict
    dropped: int      # messages removed
    capped: int       # tool results cut to an excerpt
    before: int       # estimated tokens on arrival
    after: int        # estimated tokens forwarded
    budget: int       # num_ctx - response_headroom

    def as_dict(self) -> dict:
        return {"dropped": self.dropped, "capped": self.capped, "before": self.before,
                "after": self.after, "budget": self.budget}


def _chars(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def _tokens(chars: int, cpt: float) -> int:
    return math.ceil(chars / cpt)


def _msg_tokens(msg: dict, cpt: float) -> int:
    """Estimated tokens of one message, with media parts at a flat cost."""
    content = msg.get("content")
    media = 0
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") not in (None, "text"):
                media += 1
            else:
                text_parts.append(part)
        msg = {**msg, "content": text_parts}
    return _tokens(_chars(msg), cpt) + media * MEDIA_PART_TOKENS


def _truncate_middle(text: str, max_chars: int) -> str:
    """Keep the head (70%) and tail (30%) of `text`, marking the cut."""
    head_n = int(max_chars * 0.7)
    tail_n = max_chars - head_n
    cut = len(text) - max_chars
    return text[:head_n] + TRUNCATION_MARK.format(n=cut) + text[len(text) - tail_n:]


def _rendered(msg: dict) -> Tuple[List[str], int, int]:
    """What the chat template prints for one message: (texts, markup tokens,
    media parts). Texts are the raw strings — a tool result's quotes and
    newlines are one character each here, not the two of their JSON escape."""
    texts: List[str] = []
    markup, media = MSG_MARKUP_TOKENS, 0
    content = msg.get("content")
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") not in (None, "text"):
                media += 1
            elif isinstance(part, dict):
                texts.append(str(part.get("text") or ""))
            else:
                texts.append(part if isinstance(part, str) else json.dumps(part, ensure_ascii=False))
    elif content is not None:
        texts.append(json.dumps(content, ensure_ascii=False))
    for key in ("reasoning_content", "reasoning"):
        if isinstance(msg.get(key), str):
            texts.append(msg[key])
    calls = msg.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict):
            continue
        markup += CALL_MARKUP_TOKENS
        texts.append(str(fn.get("name") or ""))
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass  # not JSON: the template gets the string as it is
        if isinstance(args, dict):
            # Rendered one parameter per block, the value raw: a file written
            # through a call costs its own tokens, not its JSON escape.
            for name, value in args.items():
                markup += ARG_MARKUP_TOKENS
                texts.append(str(name))
                texts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        elif args not in (None, ""):
            texts.append(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
    return texts, markup, media


class _CharMeter:
    """The fallback estimate: serialized JSON characters / `chars_per_token`."""

    def __init__(self, cpt: float):
        self.cpt = cpt
        self.label = f"chars/{cpt:g}"

    def bound(self, body_str: str) -> int:
        return _tokens(len(body_str), self.cpt)

    def fixed(self, payload: dict) -> int:
        # Everything that is not the conversation (tools, sampling params, …) is
        # sent regardless, so it is a fixed cost against the budget, like the
        # system prompt.
        return _tokens(_chars({k: v for k, v in payload.items() if k != "messages"}), self.cpt)

    def costs(self, messages: List[dict]) -> List[int]:
        return [_msg_tokens(m, self.cpt) for m in messages]

    def excerpts(self, texts: List[str], max_tokens: int) -> List[Optional[str]]:
        max_chars = int(max_tokens * self.cpt)
        return [_truncate_middle(t, max_chars) if len(t) > max_chars else None for t in texts]


class _TokenMeter:
    """Counts with a real tokenizer, over what the chat template renders."""

    def __init__(self, tokenizer, path: str):
        self.tok = tokenizer
        self.label = os.path.basename(path)

    def _count(self, texts: List[str]) -> List[int]:
        # encode_batch rather than a loop: it spreads over the cores and runs
        # without the GIL — about 60 ms for 1.5 MB of conversation on gw.
        if not texts:
            return []
        return [len(e.ids) for e in self.tok.encode_batch(texts, add_special_tokens=False)]

    def bound(self, body_str: str) -> int:
        # Every token covers at least one byte, so the body's UTF-8 length bounds
        # its text; the JSON around each message is longer than the markup the
        # template puts there. The preambles are the only additions.
        return len(body_str.encode("utf-8")) + PROMPT_MARKUP_TOKENS + TOOLS_PREAMBLE_TOKENS

    def fixed(self, payload: dict) -> int:
        schemas = payload.get("tools") or payload.get("functions")
        texts = (
            [json.dumps(s, ensure_ascii=False) for s in schemas] if isinstance(schemas, list) else []
        )
        return PROMPT_MARKUP_TOKENS + (TOOLS_PREAMBLE_TOKENS if texts else 0) + sum(self._count(texts))

    def costs(self, messages: List[dict]) -> List[int]:
        rendered = [_rendered(m) for m in messages]
        counts = iter(self._count([t for texts, _, _ in rendered for t in texts]))
        return [
            markup + media * MEDIA_PART_TOKENS + sum(next(counts) for _ in texts)
            for texts, markup, media in rendered
        ]

    def excerpts(self, texts: List[str], max_tokens: int) -> List[Optional[str]]:
        """Head (70%) + tail (30%) of each text over `max_tokens`, cut on token
        boundaries; None for the ones that fit."""
        out: List[Optional[str]] = [None] * len(texts)
        # A text with no more bytes than the cap cannot exceed it; skip encoding it.
        big = [i for i, t in enumerate(texts) if len(t.encode("utf-8")) > max_tokens]
        encs = self.tok.encode_batch([texts[i] for i in big], add_special_tokens=False)
        for i, enc in zip(big, encs):
            n = len(enc.ids)
            if n <= max_tokens:
                continue
            text = texts[i]
            head = int(max_tokens * 0.7)
            tail = max_tokens - head
            head_end = enc.offsets[head - 1][1] if head else 0
            # A character split over several tokens gives them overlapping offsets.
            tail_start = max(enc.offsets[n - tail][0] if tail else len(text), head_end)
            out[i] = (
                text[:head_end]
                + TRUNCATION_MARK.format(n=tail_start - head_end)
                + text[tail_start:]
            )
        return out


# The tokenizer `trim.tokenizer` names, loaded on first use (~0.4 s, ~120 MB) by
# whichever request first needs a count, then shared: (path, Tokenizer), or
# (path, None) once loading it failed, so a bad path warns once rather than per
# request. A reload that changes the path loads the new one and lets the old go.
_loaded: Tuple[str, object] = ("", None)
_load_lock = threading.Lock()


def _tokenizer(path: str):
    global _loaded
    if not path:
        return None
    if _loaded[0] == path:
        return _loaded[1]
    with _load_lock:  # trim runs in worker threads; load once, not once per thread
        if _loaded[0] != path:
            tok = None
            try:
                from tokenizers import Tokenizer
                tok = Tokenizer.from_file(path)
                logger.info("Context guardrail: counting tokens with %s", path)
            except Exception as e:  # noqa: BLE001 — any failure means "fall back"
                logger.warning(
                    "Context guardrail: cannot load tokenizer %s (%s) — estimating "
                    "with chars_per_token instead", path, e,
                )
            _loaded = (path, tok)
        return _loaded[1]


def _meter(t):
    tok = _tokenizer(t.tokenizer)
    return _TokenMeter(tok, t.tokenizer) if tok is not None else _CharMeter(t.chars_per_token)


def _cap_old_tool_results(messages: List[dict], t, meter) -> Tuple[List[dict], List[int]]:
    """Shrink tool results that are both old and oversized. Returns the messages
    and the indices of the ones excerpted."""
    if t.max_tool_result_tokens <= 0:
        return messages, []
    total = len(messages)
    old = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool"
        and isinstance(m.get("content"), str)
        and total - 1 - i >= t.protect_recent  # depth: 0 = newest
    ]
    shorts = meter.excerpts([messages[i]["content"] for i in old], t.max_tool_result_tokens)
    out = list(messages)
    excerpted = []
    for i, short in zip(old, shorts):
        if short is not None:
            out[i] = {**messages[i], "content": short}
            excerpted.append(i)
    return out, excerpted


def _blocks(indices: List[int], messages: List[dict]) -> List[List[int]]:
    """Group non-system message indices into atomic blocks.

    A block is one message, except that an assistant message carrying
    `tool_calls` absorbs the run of `tool` messages that follows it. Dropping
    history at a block boundary can therefore never leave a tool call without
    its results, or a result without its call.
    """
    blocks: List[List[int]] = []
    for i in indices:
        m = messages[i]
        if (
            m.get("role") == "tool"
            and blocks
            and (
                messages[blocks[-1][-1]].get("role") == "tool"
                or messages[blocks[-1][0]].get("tool_calls")
            )
        ):
            blocks[-1].append(i)
        else:
            blocks.append([i])
    return blocks


def trim_request(
    payload: dict, body_str: str, asked: Optional[str], request_id: Optional[int] = None
) -> Optional[Trimmed]:
    """Return the trimmed copy of `payload` with what was done to it, or None
    when it needs no change (the caller then forwards the original, untouched).

    `body_str` is the raw request body, used as a free upper bound: a body whose
    total length already fits the budget is never even inspected. `request_id`
    is the In-flight id, so the log line can be matched to its row.
    """
    t = conf.TRIM
    if not t.enabled:
        return None
    messages = payload.get("messages")
    num_ctx = payload.get("num_ctx")
    if not isinstance(messages, list) or not messages:
        return None
    if isinstance(num_ctx, bool) or not isinstance(num_ctx, int) or num_ctx <= 0:
        return None
    if not all(isinstance(m, dict) for m in messages):
        return None  # malformed; let the backend say so
    budget = num_ctx - t.response_headroom
    if budget <= 0:
        return None
    meter = _meter(t)
    if meter.bound(body_str) <= budget:
        return None

    fixed = meter.fixed(payload)
    costs = meter.costs(messages)
    before = fixed + sum(costs)
    if before <= budget:
        return None  # the raw-length bound was pessimistic (e.g. an image)

    messages, excerpted = _cap_old_tool_results(messages, t, meter)
    capped = len(excerpted)
    # Recount only what changed: tokenizing the whole history twice would double
    # the cost of every turn of a long agent session.
    for i, cost in zip(excerpted, meter.costs([messages[i] for i in excerpted])):
        costs[i] = cost
    system_idx = [i for i, m in enumerate(messages) if m.get("role") == "system"]
    other_idx = [i for i, m in enumerate(messages) if m.get("role") != "system"]
    running = fixed + sum(costs[i] for i in system_idx)

    # Greedy fill, newest block first; the newest block is kept unconditionally.
    blocks = _blocks(other_idx, messages)
    cut = len(messages)  # first non-system index that is kept
    for block in reversed(blocks):
        cost = sum(costs[i] for i in block)
        if running + cost > budget and cut < len(messages):
            break
        running += cost
        cut = block[0]

    # A `tool` message the client itself sent without its call (already broken
    # on arrival) can head the window; drop such orphans, but never empty it.
    kept_other = [i for i in other_idx if i >= cut]
    while len(kept_other) > 1 and messages[kept_other[0]].get("role") == "tool":
        kept_other.pop(0)
    kept = set(system_idx) | set(kept_other)
    new_messages = [m for i, m in enumerate(messages) if i in kept]

    after = fixed + sum(costs[i] for i in sorted(kept))
    dropped = len(messages) - len(new_messages)
    if dropped == 0 and capped == 0:
        return None

    TRIMS_TOTAL.labels(model=asked or "unknown").inc()
    logger.warning(
        "CONTEXT TRIM request #%s model '%s': estimated %d tokens by %s > budget %d "
        "(num_ctx=%d - headroom %d) -> dropped %d of %d message(s), cut %d old tool "
        "result(s) to an excerpt, forwarding ~%d tokens%s",
        request_id if request_id is not None else "?", asked, before, meter.label, budget,
        num_ctx, t.response_headroom, dropped, len(messages), capped, after,
        "" if after <= budget else " — STILL over budget, the newest turn alone does not fit",
    )
    out = dict(payload)
    out["messages"] = new_messages
    return Trimmed(out, dropped, capped, before, after, budget)
