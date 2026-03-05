"""Uniswap V4 pool read config for BSC (cNGN/USDT)."""

from .base import V4PoolReadConfig
from engine.config import settings

# BSC: token0=USDT(18 dec), token1=cNGN(6 dec), invert_price=True
UNISWAP_BSC_POOL_READ_CONFIG = V4PoolReadConfig(
    pool_manager=settings.uni_bsc_pool_manager,
    state_view=settings.uni_bsc_state_view,
    pool_address=settings.uni_bsc_pool_id,
    rpc_url=settings.bsc_rpc_url,
    token0_address=settings.usdt_bsc_address,
    token1_address=settings.cngn_bsc_address,
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    invert_price=True,
    dexscreener_chain="bsc",
)
