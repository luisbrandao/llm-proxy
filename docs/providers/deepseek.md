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

## Balance: `GET /user/balance`, Bearer. *Verified*
```
is_available                        true
balance_infos[].currency            "USD" or "CNY"
balance_infos[].total_balance       "3.87"   string
balance_infos[].granted_balance     "0.00"
balance_infos[].topped_up_balance   "3.87"
```
Amounts are strings. Freshness and rate limits are not documented.
