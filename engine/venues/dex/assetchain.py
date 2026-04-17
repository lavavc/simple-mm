"""AssetChain pool read config (watch-only price source)."""

from .pool_reader import PoolReadConfig
from engine.config import settings

# Read-only config for AssetChain price fetching (no keys, no trading)
ASSETCHAIN_POOL_READ_CONFIG = PoolReadConfig(
    rpc_url=settings.assetchain_rpc_url,
    pool_address=settings.assetchain_pool_address,
    token0_address=settings.usdt_assetchain_address,
    token1_address=settings.cngn_assetchain_address,
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    invert_price=True,
)
