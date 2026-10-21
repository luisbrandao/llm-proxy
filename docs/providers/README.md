# Provider APIs: rates, quota and balance

What each backend lets us read about **price**, **quota** and **balance**, so the proxy can keep
that information current instead of inferring it from traffic. Each page says what was checked
against the live API (marked *verified*) and what comes only from the vendor's docs.

Checked on 2026-10-21 with the proxy's own keys, read-only calls.

| | rates | quota | balance | cache price | time-of-day price |
|---|---|---|---|---|---|
| [OpenRouter](openrouter.md) | `GET /models`, `GET /models/{a}/{s}/endpoints` (public) | none, pay-as-you-go | `GET /credits`, `GET /key` | `input_cache_read` | `pricing.overrides` |
| [NanoGPT](nanogpt.md) | `GET /api/v1/models?detailed=true` | `GET /api/subscription/v1/usage` | `POST /api/check-balance` | `cacheReadInputPer1kTokens` | not seen |
| [DeepSeek](deepseek.md) | **none**, a table on the docs page | none | `GET /user/balance` | on the docs page | on the docs page |

## Units differ, and one of them is a trap

| source | field | unit |
|---|---|---|
| OpenRouter | `pricing.prompt`, `completion`, `input_cache_read` | USD **per token**, as strings |
| NanoGPT | `pricing.prompt`, `completion` | USD **per million** tokens, as numbers |
| NanoGPT | `pricing.cacheReadInputPer1kTokens` | USD **per thousand** tokens, although `unit` says `per_million_tokens` |
| DeepSeek | docs table | USD per million tokens |

The proxy's own `pricing:` table (`config.Rate`) is USD per million tokens.

## What is cheap to poll

| data | size | how often it makes sense |
|---|---|---|
| OpenRouter `/models` | 787 KB, 468 models | hourly |
| NanoGPT `/models?detailed=true` | 773 KB, 668 models | hourly |
| OpenRouter `/models/{slug}/endpoints` | 30 to 50 KB per model | hourly, only for models in use |
| NanoGPT subscription usage | under 1 KB, cached up to 60 s upstream | every minute or two |
| balances (all three) | under 1 KB | every few minutes |

Rates change rarely and are cheap to fetch, so an hourly refresh needs no traffic and notices a
price change without any request being sent to the backend.

## Findings that shaped the design

- **NanoGPT's quota counts input tokens only, and cached input counts in full.** Its own counter
  read 58.92M while the proxy's ledger summed 58.0M input tokens for the same window. Output
  tokens are not counted.
- **NanoGPT bills overage at its listed price.** `allowOverage` is true on this account, the
  request then reports `usage.cost` above zero, and in-quota requests report exactly `0`.
- **NanoGPT's `pricing` already contains its discount.** `effective_pricing` shows the list price
  and the effective one: for `deepseek-v4-pro-0813:thinking` the list is $1.32 per million input
  tokens and the effective price $0.1452, an 89% discount.
- **OpenRouter publishes DeepSeek's peak window.** The `overrides` on `deepseek-v4.1-flash-1017`
  and `deepseek-v4-pro-0813` double every rate on weekdays 01:00 to 04:00 and 06:00 to 10:00 UTC,
  which is the window in the proxy's `pricing:` table.
- **OpenRouter's `/models` price is not the price of the endpoint you will get.** Per upstream
  host the same model varies in price, quantization (fp4, fp8, unknown) and uptime, which is
  what `/endpoints` returns.
