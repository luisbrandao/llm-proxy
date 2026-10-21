# OpenRouter

Base URL `https://openrouter.ai/api/v1`. Source: OpenRouter's OpenAPI spec (version 1.0.0, 116
paths, 2 MB; docs index at `https://openrouter.ai/docs/llms.txt`), checked against live responses.

## Rates

### `GET /models`: public, no key. *Verified*
468 models, 787 KB. Per model: `id`, `canonical_slug`, `name`, `context_length`, `top_provider`,
`supported_parameters`, `pricing`.

`pricing` values are **USD per token, as strings**: `"0.000000079"` is $0.079 per million.

| key | meaning |
|---|---|
| `prompt`, `completion` | input and output |
| `input_cache_read` | cached input read |
| `input_cache_write`, `input_cache_write_1h` | cache writes, where priced |
| `internal_reasoning`, `request`, `image`, `audio`, `web_search` | other meters |
| `discount` | fraction; the price is multiplied by `1 - discount` |
| `overrides` | conditional prices, below |

For `z-ai/glm-5.3` the model-level price equalled the cheapest endpoint's price in the probe.
The spec does not say that is guaranteed, so treat it as "a price", not "the price you will pay".

### `pricing.overrides`
A list of conditional prices. An entry applies when **all** its condition fields match; later
entries win per price key; keys an entry omits inherit the base price.

| condition | meaning |
|---|---|
| `utc_days` | weekday names, evaluated at the request instant |
| `utc_start` | inclusive start of a daily UTC window, as an HHMM number (`100` is 01:00) |
| `utc_end` | exclusive end of that window (`400` is 04:00); `0` reads as end of day |
| `min_prompt_tokens` | applies when the prompt is strictly longer than this |

*Verified:* `deepseek/deepseek-v4.1-flash-1017` and `deepseek-v4-pro-0813` carry weekday windows
01:00 to 04:00 and 06:00 to 10:00 UTC at exactly twice the base on every rate. Saturday and Sunday
are base price.

### `GET /models/{author}/{slug}/endpoints`: public. *Verified*
One entry per upstream host (30 to 50 KB per model). Fields worth reading:

| field | notes |
|---|---|
| `provider_name`, `tag` | the host and its routing slug |
| `pricing` | same shape as above, per host |
| `quantization` | `fp4`, `fp8`, `fp16`, `bf16`, `unknown`, and others |
| `status` | `0` healthy; the spec lists `-1 -2 -3 -5 -10` without describing them |
| `uptime_last_5m`, `uptime_last_30m`, `uptime_last_1d` | percent |
| `throughput_last_30m`, `latency_last_30m` | percentiles `p50 p75 p90 p99`; latency is null without a key |
| `supports_implicit_caching` | whether the host caches on its own |

The same host can appear twice (two entries for InferenceNet on `glm-5.3`).

Prices differ a lot between hosts. On `deepseek/deepseek-v4.1-flash-1017`, input ranged from
$0.049 to $0.09 per million across hosts, against $0.15 on the model-level price.

## Balance and spend

| call | returns | verified |
|---|---|---|
| `GET /credits` | `data.total_credits`, `data.total_usage` (USD, cumulative; remaining is the difference) | yes |
| `GET /key` | `data.usage`, `usage_daily`, `usage_weekly`, `usage_monthly`, `limit`, `limit_remaining` | yes |
| `GET /generation?id=` | cost, tokens and serving host of one generation | no |

The spec says `/credits` needs a management key. A normal key returned 200 on 2026-10-21.
`/key` `usage_monthly` ($10.35) was consistent with the ledger's OpenRouter total for the same
weeks ($9.86 since 8 Oct), a useful cross-check.

## Routing control (request body)
The `provider` object: `order`, `only`, `ignore`, `quantizations`, `sort` (`price`, `throughput`,
`latency`), `max_price` (USD per million tokens), `allow_fallbacks`, `require_parameters`,
`data_collection`, `zdr`. Shortcuts: `:floor` sorts by price, `:nitro` by throughput.
Quantizations accepted: `int4 int8 fp4 mxfp4 nvfp4 fp6 fp8 mxfp8 fp16 bf16 fp32 unknown`.
