"""DeepSeek: its balance, and one repair its API needs on the way in.

DeepSeek publishes no rate API — its prices are a table on the docs page, kept in the
provider's `pricing:` block (see config.Pricing) — and has no subscription quota. What
it does have is `GET /user/balance`: `balance_infos[]` of strings per currency.
Endpoints: docs/providers/deepseek.md.

## Reasoning has to come back, under DeepSeek's name

In thinking mode DeepSeek returns the model's reasoning as `reasoning_content`, and a
client is expected to send it back on the assistant message that made tool calls. What
the API enforces, measured against the live service on 2026-10-21:

* A tool-call turn **DeepSeek itself wrote** is accepted whatever the client sends back;
  it recognises its own tool-call ids.
* A tool-call turn **another backend wrote** is refused with `400 The reasoning_content in
  the thinking mode must be passed back to the API` unless the message carries a
  `reasoning_content` key. Its content does not matter — the real text and an empty
  string are both accepted — but the key has to be there. NanoGPT and OpenRouter name the
  same thing `reasoning`, which does not count, and a client that never saw a
  `reasoning_content` field sends back neither.
* Plain assistant turns are not checked, and the other direction is harmless: nano and
  OpenRouter accept DeepSeek's history with or without the field.

Smart routing and failover both move a conversation between these backends, so the first
request after a move to DeepSeek direct used to die with that 400. `adapt_request` puts
the key on every tool-call assistant message that lacks it, carrying the reasoning text
across from the other backends' spelling when there is any.
"""
from typing import Optional

from app.providers import Balance, to_float


def _reasoning_text(message: dict) -> str:
    """The reasoning another backend attached to this turn, under whatever name: OpenRouter
    and NanoGPT's `reasoning`, or OpenRouter's structured `reasoning_details`."""
    text = message.get("reasoning")
    if isinstance(text, str) and text:
        return text
    parts = []
    for detail in message.get("reasoning_details") or []:
        if isinstance(detail, dict) and isinstance(detail.get("text"), str):
            parts.append(detail["text"])
    return "".join(parts)


def adapt_request(payload: dict) -> dict:
    """The request with `reasoning_content` present on every tool-call assistant message.

    Copy-on-write: the payload is shared with the failover targets that have not been
    tried yet, which must still get the client's own history, so nothing is mutated and
    the same object comes back when there is nothing to repair."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload
    repaired = None
    for i, m in enumerate(messages):
        if (isinstance(m, dict) and m.get("role") == "assistant"
                and m.get("tool_calls") and "reasoning_content" not in m):
            if repaired is None:
                repaired = list(messages)
            repaired[i] = {**m, "reasoning_content": _reasoning_text(m)}
    if repaired is None:
        return payload
    return {**payload, "messages": repaired}


def parse_balance(payload) -> Optional[Balance]:
    infos = [i for i in (payload or {}).get("balance_infos") or [] if isinstance(i, dict)]
    # The account's USD wallet when there is one, otherwise whatever it holds.
    info = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else None)
    amount = to_float(info.get("total_balance")) if info else None
    return Balance(currency=str(info.get("currency") or "USD"), amount=amount) if amount is not None else None


async def fetch_balance(client, provider) -> Optional[Balance]:
    resp = await client.get(
        provider.base_url.rstrip("/") + "/user/balance",
        headers={"Authorization": f"Bearer {provider.api_key}", "User-Agent": "llm-proxy"},
    )
    resp.raise_for_status()
    return parse_balance(resp.json())
