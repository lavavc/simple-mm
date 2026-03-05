"""Uniswap V4 pool read config for Base (cNGN/USDC)."""

from .base import V4PoolReadConfig
from engine.config import settings

# Base: token0=cNGN(6 dec), token1=USDC(6 dec), invert_price=False
UNISWAP_BASE_POOL_READ_CONFIG = V4PoolReadConfig(
    pool_manager=settings.uni_base_pool_manager,
    state_view=settings.uni_base_state_view,
    pool_address=settings.uni_base_pool_id,
    rpc_url=settings.base_rpc_url,
    token0_address=settings.cngn_base_address,
    token1_address=settings.usdc_base_address,
    token0_symbol="cNGN",
    token1_symbol="USDC",
    token0_decimals=6,
    token1_decimals=6,
    invert_price=False,
    dexscreener_chain="base",
)
