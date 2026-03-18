"""Dynamic gas cost oracle.

Gas units are fixed (measured from real on-chain swaps).
Gas price and native token USD price are refreshed every ~30 s.

If prices cannot be fetched, accessors return None and trading is blocked
by the callers. The scheduler fires an alert on any fetch failure.

Requires ALCHEMY_KEY in settings for native token prices.
Gas prices are fetched from each chain via eth_gasPrice.
"""

import asyncio
from decimal import Decimal
import structlog
import httpx
from web3 import AsyncWeb3

logger = structlog.get_logger()

# Fixed gas units from real on-chain measurements.
GAS_UNITS_BASE: int = 173_000   # Uniswap V4 on Base
GAS_UNITS_BSC: int = 158_000    # PancakeSwap V3 on BSC

# State is empty until first successful fetch. None means "not yet fetched".
_state: dict[str, Decimal] = {}


def gas_usd_base() -> Decimal | None:
    return _state.get("gas_usd_base")


def gas_usd_bsc() -> Decimal | None:
    return _state.get("gas_usd_bsc")


async def _fetch_gas_price_gwei(rpc_url: str) -> Decimal:
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    wei = await w3.eth.gas_price
    return Decimal(wei) / Decimal(10**9)


async def _fetch_native_prices(alchemy_key: str) -> tuple[Decimal, Decimal]:
    url = f"https://api.g.alchemy.com/prices/v1/{alchemy_key}/tokens/by-symbol"
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(url, params={"symbols": ["ETH", "BNB"]})
        resp.raise_for_status()
        prices = {item["symbol"]: Decimal(item["prices"][0]["value"]) for item in resp.json()["data"]}
        return prices["ETH"], prices["BNB"]


async def update() -> None:
    """Refresh gas prices and native token USD prices.

    Raises RuntimeError if any fetch fails — callers should catch this,
    fire an alert, and leave _state unchanged (preserving last known good values,
    or empty on first run which blocks trading).
    """
    from engine.config import settings  # late import to avoid circular deps

    try:
        gas_gwei_base, gas_gwei_bsc, (eth_usd, bnb_usd) = await asyncio.gather(
            _fetch_gas_price_gwei(settings.base_rpc_url),
            _fetch_gas_price_gwei(settings.bsc_rpc_url),
            _fetch_native_prices(settings.alchemy_key),
        )
    except Exception as e:
        raise RuntimeError(f"gas_oracle_fetch_failed: {e}") from e

    _state["eth_usd"] = eth_usd
    _state["bnb_usd"] = bnb_usd
    _state["gas_usd_base"] = gas_gwei_base * GAS_UNITS_BASE / Decimal(10**9) * eth_usd
    _state["gas_usd_bsc"]  = gas_gwei_bsc  * GAS_UNITS_BSC  / Decimal(10**9) * bnb_usd

    logger.info(
        "gas_oracle_updated",
        gas_gwei_base=float(gas_gwei_base),
        gas_gwei_bsc=float(gas_gwei_bsc),
        eth_usd=float(eth_usd),
        bnb_usd=float(bnb_usd),
        gas_usd_base=float(_state["gas_usd_base"]),
        gas_usd_bsc=float(_state["gas_usd_bsc"]),
    )
