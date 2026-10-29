# NanoGPT

Hosts: `https://api.nano-gpt.com` for API-key integrations, `https://nano-gpt.com` also answers.
Index of all pages: `https://docs.nano-gpt.com/llms.txt`.

## Rates

### `GET /api/v1/models?detailed=true`: key optional. *Verified*
668 models, 773 KB. Auth, if sent, is `Authorization: Bearer` or `x-api-key`; a key can change
pricing to the account's own.

| field | unit |
|---|---|
| `pricing.prompt`, `pricing.completion` | USD per **million** tokens |
| `pricing.cacheReadInputPer1kTokens` | USD per **thousand** tokens, though `pricing.unit` says per million |
| `pricing.cacheWriteInputPer1kTokens` | the same, where present |
| `pricing.currency`, `pricing.unit` | `USD`, `per_million_tokens` |
| `cost_estimate` | a per-request estimate, unit not documented |
| `effective_pricing` | present on some models, below |

The per-thousand unit was confirmed: `3e-06` on the DeepSeek flash entry is $0.003 per million,
DeepSeek's own cache-hit price.

`effective_pricing` holds `list_price`, `effective_price` (both per 1k tokens), `discount_percentage`,
`pricing_type` and `source`. `pricing` already shows the **effective** price. Example on
2026-10-21, `deepseek/deepseek-v4-pro-0813:thinking`: list $1.32 per million input, effective
$0.1452, 89% discount.

Variants: `GET /api/subscription/v1/models` lists only subscription-included models,
`GET /api/paid/v1/models` the rest. Both take `?detailed=true`.

### `GET /api/models/{canonicalId}/providers`: not verified
Per-upstream-host prices, in USD per **thousand** tokens, with `quantization`, `tps`, `ttftMs`,
`uptime`. Slashes in the id are encoded as `%2F`. Note: `/api/models/...`, not `/api/v1/...`.

## Subscription quota

### `GET https://api.nano-gpt.com/api/subscription/v1/usage`: Bearer or `x-api-key`. *Verified*

```
limits.weeklyInputTokens          60000000
weeklyInputTokens.used            58920726
weeklyInputTokens.remaining       1079274
weeklyInputTokens.percentUsed     0.982        a fraction; can exceed 1
weeklyInputTokens.resetAt         1792972800000   epoch ms
dailyInputTokens                  null         not configured on this account
allowOverage                      true
period.currentPeriodEnd           2026-11-19T13:10:41Z
routing.recommendedMode           subscription | paygo | unavailable
routing.billingMode               both
```

- **Only input tokens count, and cached input counts in full.** The ledger summed 58.0M input
  tokens for the same window against nano's 58.92M.
- `resetAt` `1792972800000` is Monday 00:00 UTC, which is Sunday 21:00 BRT.
- A null counter with `degraded: true` means unknown, never "full allowance".
- **Do not trust `routing.recommendedMode` as proof of allowance.** On 2026-10-21 at 14:16 BRT the
  response read `used 60001719`, `remaining 0`, and still `recommendedMode: subscription`,
  `reason: included_quota_available`, while requests were already being billed (the first billed
  request was at 17:12:50 UTC). The counter is right; the verdict lags. The proxy uses the verdict only to veto.
- **The counter does not always reach the limit.** A request that does not fit in what is left is
  billed and not counted, so the counter can park just under the limit until the reset: on
  2026-10-28 from 19:04 BRT it read `used 59999179`, `remaining 821`, verdict `subscription`,
  while every ~70K-token request was billed. The proxy therefore treats the allowance as spent
  once less than 1% of the limit is left (`nanogpt.SPENT_BELOW`; 600K tokens, above the largest
  request seen).
- A management-scoped variant exists at `https://nano-gpt.com/api/management/v1/subscription/usage`
  with a separate token (`usage:read`); it is not needed for reading quota with the inference key.

## Overage and balance

- With `allowOverage: true`, requests past the quota are billed from the USD balance at the
  effective price. Verified from the ledger: in-quota requests report `usage.cost` of `0`, over-quota
  ones report the real amount (8 to 11 Oct: 175 requests, 15.6M tokens, $4.42).
- A key saved as *Subscription only* is rejected instead of billed.
- `POST https://api.nano-gpt.com/api/check-balance` with header `x-api-key`: `usd_balance`
  (string), `nano_balance` (string). *Verified:* `15.99945128`. Bearer is not documented for it.

## Limits
25 requests per second. Per-key optional daily limits answer `429` with `Retry-After`.
No refresh cadence is documented for model prices.

## Other endpoints, not used
`GET /api/v1/usage` (aggregate spend by day, `from`/`to`, UTC, 366 days) and
`GET /api/v1/usage/requests/{id}` (cost of one request, 24 hours).
