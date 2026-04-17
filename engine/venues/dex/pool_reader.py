"""Read-only config for pool price fetching (used by AssetChain and pool_state)."""

from dataclasses import dataclass


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
