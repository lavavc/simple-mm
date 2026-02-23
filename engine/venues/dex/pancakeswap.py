"""PancakeSwap V3 DEX adapter for BSC (BNB Chain)."""

from .base import BaseDexAdapter, PoolConfig, PoolReadConfig
from .abis import PANCAKESWAP_POOL_ABI, PANCAKESWAP_NFT_MANAGER_ABI, PANCAKESWAP_ROUTER_ABI
from engine.api.schemas import DexParams
from engine.config import settings


# ── Pool configuration ──────────────────────────────────────────────────
# On-chain token ordering: token0=USDT(0x55d3...) < token1=cNGN(0xa8ae...)
# cNGN on BSC has 6 decimals; USDT on BSC has 18 decimals.
PANCAKESWAP_CNGN_USDT_CONFIG = PoolConfig(
    chain_id=56,
    chain_name="bsc",
    rpc_url=settings.bsc_rpc_url,
    pool_address=settings.pancakeswap_pool_address,
    nft_manager_address=settings.pancakeswap_nft_manager_address,
    router_address=settings.pancakeswap_router_address,
    token0_address=settings.usdt_bsc_address,  # USDT (lower address = token0)
    token1_address=settings.cngn_bsc_address,  # cNGN (higher address = token1)
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    tick_spacing=200,
    pool_fee=10000,  # 1% fee tier (tick_spacing=200 → fee=10000 in PancakeSwap V3)
    invert_price=True,  # Native price is cNGN/USDT (~1340); we want USDT/cNGN (~0.000747)
)


# ── Read-only config for price fetching (no keys needed) ────────────────
# On-chain: token0=USDT(18 dec), token1=cNGN(6 dec)
# Native price direction: cNGN per USDT (≈ 1437)
# We want: USD per cNGN (≈ 0.0007), so invert_price=True
PANCAKESWAP_POOL_READ_CONFIG = PoolReadConfig(
    rpc_url=settings.bsc_rpc_url,
    pool_address=settings.pancakeswap_pool_address,
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    invert_price=True,
)


class PancakeSwapAdapter(BaseDexAdapter):
    """PancakeSwap V3 DEX adapter on BSC."""

    name = "pancakeswap"

    def __init__(
        self,
        lp_private_key: str,
        trade_private_key: str | None = None,
        params: DexParams | None = None,
        rpc_url: str | None = None,
    ):
        if params is None:
            params = DexParams()
        if trade_private_key is None:
            trade_private_key = lp_private_key

        config = PANCAKESWAP_CNGN_USDT_CONFIG
        if rpc_url:
            config = PoolConfig(
                chain_id=config.chain_id,
                chain_name=config.chain_name,
                rpc_url=rpc_url,
                pool_address=config.pool_address,
                nft_manager_address=config.nft_manager_address,
                router_address=config.router_address,
                token0_address=config.token0_address,
                token1_address=config.token1_address,
                token0_symbol=config.token0_symbol,
                token1_symbol=config.token1_symbol,
                token0_decimals=config.token0_decimals,
                token1_decimals=config.token1_decimals,
                tick_spacing=config.tick_spacing,
                pool_fee=config.pool_fee,
            )

        super().__init__(
            pool_config=config,
            lp_private_key=lp_private_key,
            trade_private_key=trade_private_key,
            strategy_params=params,
        )

    def get_pool_abi(self) -> list:
        return PANCAKESWAP_POOL_ABI

    def get_nft_manager_abi(self) -> list:
        return PANCAKESWAP_NFT_MANAGER_ABI

    def get_router_abi(self) -> list:
        return PANCAKESWAP_ROUTER_ABI
