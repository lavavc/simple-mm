"""PancakeSwap V3 DEX adapter for BSC (BNB Chain).

Currently only provides read-only pool configuration for price fetching.
Full trading adapter can be added later when BSC keys are available.
"""

from .base import PoolConfig, PoolReadConfig
from engine.config import settings


# ── Token addresses on BSC ──────────────────────────────────────────────
CNGN_BSC = "0xa8aea66b361a8d53e8865c62d142167af28af058"
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"

# ── PancakeSwap V3 infrastructure on BSC ────────────────────────────────
PANCAKESWAP_NFT_MANAGER = "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"
PANCAKESWAP_ROUTER = "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4"

# ── Pool configuration ──────────────────────────────────────────────────
# On-chain token ordering: token0=USDT(0x55d3...) < token1=cNGN(0xa8ae...)
# cNGN on BSC has 6 decimals; USDT on BSC has 18 decimals.
PANCAKESWAP_CNGN_USDT_CONFIG = PoolConfig(
    chain_id=56,
    chain_name="bsc",
    rpc_url=settings.bsc_rpc_url,
    pool_address="0xb84e7c912a1034ad674bba8859fca84f1f614a29",
    nft_manager_address=PANCAKESWAP_NFT_MANAGER,
    router_address=PANCAKESWAP_ROUTER,
    token0_address=USDT_BSC,  # USDT (lower address = token0)
    token1_address=CNGN_BSC,  # cNGN (higher address = token1)
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    tick_spacing=200,  # PancakeSwap V3 tick spacing (2500 bps fee tier)
)


# ── Read-only config for price fetching (no keys needed) ────────────────
# On-chain: token0=USDT(18 dec), token1=cNGN(6 dec)
# Native price direction: cNGN per USDT (≈ 1437)
# We want: USD per cNGN (≈ 0.0007), so invert_price=True
PANCAKESWAP_POOL_READ_CONFIG = PoolReadConfig(
    rpc_url=settings.bsc_rpc_url,
    pool_address="0xb84e7c912a1034ad674bba8859fca84f1f614a29",
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    invert_price=True,
)
