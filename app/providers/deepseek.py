"""DeepSeek: balance only.

DeepSeek publishes no rate API — its prices are a table on the docs page, kept in the
provider's `pricing:` block (see config.Pricing) — and has no subscription quota. What
it does have is `GET /user/balance`: `balance_infos[]` of strings per currency.
Endpoints: docs/providers/deepseek.md.
"""
from typing import Optional

from app.providers import Balance, to_float


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
