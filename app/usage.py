"""What a backend's response says the request consumed — and what it charged.

Every OpenAI-compatible backend reports `usage.prompt_tokens` and
`usage.completion_tokens`. The interesting fields past those two are *not*
standardized, and each vendor spells them differently:

    cost          OpenRouter and NanoGPT put the price in `usage.cost` (USD);
                  NanoGPT also repeats it in a top-level `x_nanogpt_pricing`.
    cached        prompt tokens served from a prompt cache — OpenAI-style
                  `prompt_tokens_details.cached_tokens` (OpenRouter, NanoGPT,
                  Google), Anthropic-style `cache_read_input_tokens` (NanoGPT
                  repeats it), DeepSeek's `prompt_cache_hit_tokens`.
    cache_write   prompt tokens written into the cache — `cache_write_tokens`
                  (OpenRouter) / `created_cache_tokens` (NanoGPT) inside
                  `prompt_tokens_details`, or Anthropic's
                  `cache_creation_input_tokens`.
    reasoning     completion tokens spent thinking — `completion_tokens_details
                  .reasoning_tokens`, or NanoGPT's flat `reasoning_tokens`.

`parse` folds all of those spellings into one `Usage`, so the handlers, the
request log, the metrics, the In-flight row and the cost ledger all read the same
object instead of each fishing in the dict. A field a backend does not report is
0 — except `cost`, which stays None: "the backend did not say" and "the backend
said it was free" (OpenRouter reports `cost: 0` for its free models) are
different facts, and the ledger keeps them apart.

A leaf module: nothing here imports from the app, so it is safe anywhere.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    cache_write: int = 0
    reasoning: int = 0
    # What the backend says this request cost, in `currency`. None = not reported.
    cost: Optional[float] = None
    currency: Optional[str] = None

    @property
    def priced(self) -> bool:
        return self.cost is not None


def _int(value) -> int:
    """A token count as an int; anything that is not a finite number is 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value == value else 0  # NaN check
    return 0


def _best(*candidates) -> int:
    """The largest of the counts present. Vendors that report the same quantity
    under two names (NanoGPT: `cached_tokens` and `cache_read_input_tokens`)
    agree in practice; taking the max means a stale zero in one spelling cannot
    hide the real number in the other."""
    return max((_int(c) for c in candidates), default=0)


def _float(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    return None


def parse(data) -> Optional[Usage]:
    """The `Usage` a completion (or a streamed chunk) carries, or None when it
    carries no `usage` block at all — a mid-stream delta, an error body.

    Takes the whole response object, not just `usage`, because NanoGPT's pricing
    block is a sibling of `usage`, not a child.
    """
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    pricing = data.get("x_nanogpt_pricing")
    if not isinstance(pricing, dict):
        pricing = {}

    cost = _float(usage.get("cost"))
    if cost is None:
        cost = _float(pricing.get("cost"))
    currency = None
    if cost is not None:
        raw = usage.get("currency") or pricing.get("currency") or "USD"
        currency = str(raw).upper()

    return Usage(
        prompt=_int(usage.get("prompt_tokens")),
        completion=_int(usage.get("completion_tokens")),
        cached=_best(
            prompt_details.get("cached_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("prompt_cache_hit_tokens"),
        ),
        cache_write=_best(
            prompt_details.get("cache_write_tokens"),
            prompt_details.get("created_cache_tokens"),
            usage.get("cache_creation_input_tokens"),
        ),
        reasoning=_best(
            completion_details.get("reasoning_tokens"),
            usage.get("reasoning_tokens"),
        ),
        cost=cost,
        currency=currency,
    )


def format_cost(cost: Optional[float]) -> Optional[str]:
    """A cost as a plain decimal for the request log: enough digits that a
    sub-millicent request does not round to zero, no trailing zeros, never
    scientific notation (Loki's `| logfmt` reads it as a number)."""
    if cost is None:
        return None
    text = f"{cost:.8f}".rstrip("0").rstrip(".")
    return text or "0"
