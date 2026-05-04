"""Uniswap V4 Base configs and swap execution adapter."""

import dataclasses

from engine.types import V4PoolReadConfig
from .v4 import BaseV4DexAdapter, V4ExecutionConfig
from engine.config import DexParams
from engine.config import settings, Settings

_BASE_POSITION_MANAGER = "0x7c5f5a4bbd8fd63184577525326123b519429bdc"

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
    chain_id_str="base",
)

UNISWAP_BASE_EXECUTION_CONFIG = V4ExecutionConfig(
    chain_id=8453,
    chain_name="base",
    rpc_url=settings.base_rpc_url,
    pool_manager=settings.uni_base_pool_manager,
    state_view=settings.uni_base_state_view,
    pool_id=settings.uni_base_pool_id,
    universal_router=settings.uni_base_universal_router,
    permit2=settings.permit2_address,
    token0_address=settings.cngn_base_address,
    token1_address=settings.usdc_base_address,
    token0_symbol="cNGN",
    token1_symbol="USDC",
    token0_decimals=6,
    token1_decimals=6,
    fee=1500,
    tick_spacing=30,
    hooks="0x0000000000000000000000000000000000000000",
    invert_price=False,
    cngn_is_token0=True,
    position_manager=_BASE_POSITION_MANAGER,
)


class UniswapBaseV4Adapter(BaseV4DexAdapter):
    name = "uni-base"

    def __init__(
        self,
        lp_private_key: str,
        trade_private_key: str | None = None,
        params: DexParams | None = None,
        rpc_url: str | None = None,
        _settings: Settings = settings,
    ):
        if params is None:
            params = _settings.uni_base_lp_params
        if trade_private_key is None:
            trade_private_key = lp_private_key

        config = UNISWAP_BASE_EXECUTION_CONFIG
        if rpc_url:
            config = dataclasses.replace(config, rpc_url=rpc_url)

        super().__init__(
            config=config,
            lp_private_key=lp_private_key,
            trade_private_key=trade_private_key,
            strategy_params=params,
        )
