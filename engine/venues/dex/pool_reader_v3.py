"""Read-only price reader for UniswapV3-style concentrated liquidity pools."""

import time as _time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import structlog
from web3 import Web3

from engine.types import PriceQuote
from .shared import sqrt_price_x96_to_decimal

logger = structlog.get_logger()

# Function selector for slot0(): keccak256("slot0()")[:4] = 0x3850c7bd
SLOT0_SELECTOR = bytes.fromhex("3850c7bd")

# Function selector for liquidity(): keccak256("liquidity()")[:4] = 0x1a686502
LIQUIDITY_SELECTOR = bytes.fromhex("1a686502")

# DexScreener chain identifiers
DEXSCREENER_CHAIN_MAP = {8453: "base", 56: "bsc"}


@dataclass
class PoolReadConfig:
    """Minimal config for read-only pool price fetching (no keys needed).

    token0/token1 must match the actual on-chain ordering (token0 < token1 by address).
    Set invert_price=True when the pool's native price direction is opposite to what
    the consumer expects.
    """

    rpc_url: str
    pool_address: str
    token0_address: str
    token1_address: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    invert_price: bool = False


class PoolPriceReader:
    """Read-only price reader for any UniswapV3-style concentrated liquidity pool.

    Uses raw eth_call with the slot0() selector — protocol-agnostic since all
    V3 forks return sqrtPriceX96 as the first word regardless of total field count.
    No private keys, no NFT manager, no router.
    """

    def __init__(self, config: PoolReadConfig, source_name: str):
        self.config = config
        self.source_name = source_name
        self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        self._pool_address = Web3.to_checksum_address(config.pool_address)

    def get_price(self) -> Optional[PriceQuote]:
        """Read the current pool price via a single slot0() RPC call."""
        try:
            result = self._w3.eth.call(
                {"to": self._pool_address, "data": SLOT0_SELECTOR}
            )

            if len(result) < 64:
                logger.error(
                    "slot0_response_too_short",
                    source=self.source_name,
                    length=len(result),
                )
                return None

            sqrt_price_x96 = int.from_bytes(result[:32], "big")
            mid = sqrt_price_x96_to_decimal(
                sqrt_price_x96,
                self.config.token0_decimals,
                self.config.token1_decimals,
            )

            if mid <= 0:
                return None

            if self.config.invert_price:
                mid = Decimal(1) / mid

            return PriceQuote(
                source=f"{self.source_name}_pool",
                timestamp=int(_time.time() * 1000),
                bid=mid,
                ask=mid,
                mid=mid,
            )
        except Exception as e:
            logger.error(
                "pool_price_read_failed",
                source=self.source_name,
                pool=self.config.pool_address,
                error=str(e),
            )
            return None
