"""Aerodrome DEX adapter for Base chain."""

from .base import BaseDexAdapter, PoolConfig
from .abis import POOL_ABI, NFT_POSITION_MANAGER_ABI, ROUTER_ABI
from engine.api.schemas import DexParams
from engine.config import settings


# Aerodrome cNGN/USDC pool configuration on Base
AERODROME_CNGN_USDC_CONFIG = PoolConfig(
    chain_id=8453,
    chain_name="base",
    rpc_url=settings.base_rpc_url,
    pool_address="0x0206B696a410277eF692024C2B64CcF4EaC78589",
    nft_manager_address="0x827922686190790b37229fd06084350E74485b72",
    router_address="0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5",
    token0_address="0x46C85152bFe9f96829aA94755D9f915F9B10EF5F",  # cNGN
    token1_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    token0_symbol="cNGN",
    token1_symbol="USDC",
    token0_decimals=18,
    token1_decimals=6,
    tick_spacing=100,  # Aerodrome tick spacing for this fee tier
)


class AerodromeAdapter(BaseDexAdapter):
    """Aerodrome DEX adapter on Base chain."""

    name = "aerodrome"

    def __init__(
        self,
        lp_private_key: str,
        trade_private_key: str,
        params: DexParams | None = None,
    ):
        """
        Initialize Aerodrome adapter.

        Args:
            lp_private_key: Private key for LP position management
            trade_private_key: Private key for market-making swaps
            params: DEX strategy parameters (uses defaults if not provided)
        """
        if params is None:
            params = DexParams()

        super().__init__(
            pool_config=AERODROME_CNGN_USDC_CONFIG,
            lp_private_key=lp_private_key,
            trade_private_key=trade_private_key,
            strategy_params=params,
        )

    def get_pool_abi(self) -> list:
        """Return Aerodrome pool ABI."""
        return POOL_ABI

    def get_nft_manager_abi(self) -> list:
        """Return Aerodrome NFT position manager ABI."""
        return NFT_POSITION_MANAGER_ABI

    def get_router_abi(self) -> list:
        """Return Aerodrome swap router ABI."""
        return ROUTER_ABI
