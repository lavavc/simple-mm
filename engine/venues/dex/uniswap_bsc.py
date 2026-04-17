"""Uniswap V4 BSC configs and swap execution adapter."""

import dataclasses

from .shared import V4PoolReadConfig
from .v4 import BaseV4DexAdapter, V4ExecutionConfig
from engine.config import DexParams
from engine.config import settings, Settings

_BSC_POSITION_MANAGER = "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b"

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
    chain_id_str="bsc",
)

UNISWAP_BSC_EXECUTION_CONFIG = V4ExecutionConfig(
    chain_id=56,
    chain_name="bsc",
    rpc_url=settings.bsc_rpc_url,
    pool_manager=settings.uni_bsc_pool_manager,
    state_view=settings.uni_bsc_state_view,
    pool_id=settings.uni_bsc_pool_id,
    universal_router=settings.uni_bsc_universal_router,
    permit2=settings.permit2_address,
    token0_address=settings.usdt_bsc_address,
    token1_address=settings.cngn_bsc_address,
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    fee=1200,
    tick_spacing=24,
    hooks="0x0000000000000000000000000000000000000000",
    invert_price=True,
    position_manager=_BSC_POSITION_MANAGER,
)


class UniswapBscV4Adapter(BaseV4DexAdapter):
    name = "uni-bsc"

    def __init__(
        self,
        lp_private_key: str,
        trade_private_key: str | None = None,
        params: DexParams | None = None,
        rpc_url: str | None = None,
        _settings: Settings = settings,
    ):
        if params is None:
            params = _settings.uni_bsc_lp_params
        if trade_private_key is None:
            trade_private_key = lp_private_key

        config = UNISWAP_BSC_EXECUTION_CONFIG
        if rpc_url:
            config = dataclasses.replace(config, rpc_url=rpc_url)

        super().__init__(
            config=config,
            lp_private_key=lp_private_key,
            trade_private_key=trade_private_key,
            strategy_params=params,
        )
