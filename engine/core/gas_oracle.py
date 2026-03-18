"""Dynamic gas cost oracle.

Gas units are fixed (measured from real on-chain swaps).
Gas price and native token USD price are refreshed every ~30 s.

Requires ALCHEMY_KEY in settings for native token prices.
Gas prices are fetched from each chain via eth_gasPrice.

Consumers call gas_usd_base() / gas_usd_bsc() — always return the latest
estimate, falling back to safe defaults if a fetch fails.
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

# Safe fallbacks — used until first successful fetch and on any fetch failure.
_DEFAULT = {
    "eth_usd": Decimal("2500"),
    "bnb_usd": Decimal("600"),
    "gas_gwei_base": Decimal("0.005"),
    "gas_gwei_bsc": Decimal("0.05"),
}

_state: dict[str, Decimal] = {
    "gas_usd_base": _DEFAULT["gas_gwei_base"] * GAS_UNITS_BASE / Decimal(10**9) * _DEFAULT["eth_usd"],
    "gas_usd_bsc":  _DEFAULT["gas_gwei_bsc"]  * GAS_UNITS_BSC  / Decimal(10**9) * _DEFAULT["bnb_usd"],
}


def gas_usd_base() -> Decimal:
    return _state["gas_usd_base"]


def gas_usd_bsc() -> Decimal:
    return _state["gas_usd_bsc"]


async def _fetch_gas_price_gwei(rpc_url: str) -> Decimal | None:
    try:
        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        wei = await w3.eth.gas_price
        return Decimal(wei) / Decimal(10**9)
    except Exception as e:
        logger.warning("gas_price_fetch_failed", rpc=rpc_url, error=str(e))
        return None


async def _fetch_native_prices(alchemy_key: str) -> tuple[Decimal, Decimal] | None:
    url = f"https://api.g.alchemy.com/prices/v1/{alchemy_key}/tokens/by-symbol"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url, params={"symbols": ["ETH", "BNB"]})
            resp.raise_for_status()
            prices = {item["symbol"]: Decimal(item["prices"][0]["value"]) for item in resp.json()["data"]}
            return prices["ETH"], prices["BNB"]
    except Exception as e:
        logger.warning("native_prices_fetch_failed", error=str(e))
        return None


async def update() -> None:
    """Refresh gas prices and native token USD prices. Called by the scheduler every ~30 s."""
    from engine.config import settings  # late import to avoid circular deps

    gas_gwei_base, gas_gwei_bsc, native_prices = await asyncio.gather(
        _fetch_gas_price_gwei(settings.base_rpc_url),
        _fetch_gas_price_gwei(settings.bsc_rpc_url),
        _fetch_native_prices(settings.alchemy_key),
    )

    gwei_base = gas_gwei_base or _DEFAULT["gas_gwei_base"]
    gwei_bsc  = gas_gwei_bsc  or _DEFAULT["gas_gwei_bsc"]

    if native_prices:
        eth_usd, bnb_usd = native_prices
    else:
        eth_usd = _state.get("eth_usd", _DEFAULT["eth_usd"])
        bnb_usd = _state.get("bnb_usd", _DEFAULT["bnb_usd"])

    _state["eth_usd"] = eth_usd
    _state["bnb_usd"] = bnb_usd
    _state["gas_usd_base"] = gwei_base * GAS_UNITS_BASE / Decimal(10**9) * eth_usd
    _state["gas_usd_bsc"]  = gwei_bsc  * GAS_UNITS_BSC  / Decimal(10**9) * bnb_usd

    logger.info(
        "gas_oracle_updated",
        gas_gwei_base=float(gwei_base),
        gas_gwei_bsc=float(gwei_bsc),
        eth_usd=float(eth_usd),
        bnb_usd=float(bnb_usd),
        gas_usd_base=float(_state["gas_usd_base"]),
        gas_usd_bsc=float(_state["gas_usd_bsc"]),
    )
