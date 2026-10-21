# DeepSeek

Base URL `https://api.deepseek.com`. Docs: `https://api-docs.deepseek.com`.

## Rates: there is no API for them
DeepSeek publishes prices only as a table on its docs page, so the proxy keeps them in the
provider's `pricing:` block in `config.yaml` (USD per million tokens, keyed by native id).
Checked against the docs table on 2026-10-21:

| model (native id) | version | input, cache hit | input, cache miss | output |
|---|---|---|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash-1017 | $0.003 | $0.15 | $0.60 |
| `deepseek-v4-pro` | DeepSeek-V4-Pro-0813 | $0.022 | $0.66 | $1.98 |

Peak prices are exactly 2x on every rate. The table does not show the peak hours; OpenRouter's
`pricing.overrides` for the same models do: weekdays 01:00 to 04:00 and 06:00 to 10:00 UTC.
The legacy names `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` are still accepted and
billed at the Flash price.

## Usage in responses
`usage` carries `prompt_tokens` (equal to hit plus miss), `prompt_cache_hit_tokens`,
`prompt_cache_miss_tokens`, `prompt_tokens_details.cached_tokens` (the same as the hit count),
`completion_tokens`, `completion_tokens_details.reasoning_tokens`. **No cost field**, so the
proxy prices each request itself from the table above.

## Thinking mode: reasoning must come back, under DeepSeek's name
In thinking mode the reply carries the model's reasoning as `reasoning_content`, and a client sends
it back on the assistant message that made tool calls. Measured against the live API on 2026-10-21:

| history the client sends | result on DeepSeek direct |
|---|---|
| a tool-call turn DeepSeek wrote, with or without `reasoning_content` | 200 either way |
| a tool-call turn **nano or OpenRouter wrote**, with their `reasoning` field only | **400** `The reasoning_content in the thinking mode must be passed back to the API` |
| the same, with nothing at all | **400** |
| the same, with `reasoning_content` set to the reasoning text | 200 |
| the same, with `reasoning_content` set to an empty string | 200 |
| a plain assistant answer (no tool call) without the field | 200 |

So the key has to be present on tool-call assistant messages; its content is not checked. OpenRouter
and NanoGPT call the same thing `reasoning` (OpenRouter also sends `reasoning_details`), and the other
direction is harmless: both accept DeepSeek's history with or without `reasoning_content`.

The proxy repairs this on the way to a DeepSeek backend (`app/providers/deepseek.py`,
`adapt_request`), because smart routing and failover both move a conversation between backends.

## Balance: `GET /user/balance`, Bearer. *Verified*
```
is_available                        true
balance_infos[].currency            "USD" or "CNY"
balance_infos[].total_balance       "3.87"   string
balance_infos[].granted_balance     "0.00"
balance_infos[].topped_up_balance   "3.87"
```
Amounts are strings. Freshness and rate limits are not documented.
