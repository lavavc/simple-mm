"""Aerodrome DEX adapter for Base chain."""

from .base import BaseDexAdapter, PoolConfig, PoolReadConfig
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
    token0_decimals=6,
    token1_decimals=6,
    tick_spacing=100,  # Aerodrome tick spacing for this fee tier
)


# Read-only config for price fetching (no keys needed)
# On-chain: token0=cNGN(0x46C8..., 6 dec), token1=USDC(0x8335..., 6 dec)
# Price direction: USDC per cNGN (≈ 0.0007) -- no inversion needed
AERODROME_POOL_READ_CONFIG = PoolReadConfig(
    rpc_url=settings.base_rpc_url,
    pool_address="0x0206B696a410277eF692024C2B64CcF4EaC78589",
    token0_symbol="cNGN",
    token1_symbol="USDC",
    token0_decimals=6,
    token1_decimals=6,
)


class AerodromeAdapter(BaseDexAdapter):
    """Aerodrome DEX adapter on Base chain."""

    name = "aerodrome"

    def __init__(
        self,
        lp_private_key: str,
        trade_private_key: str | None = None,
        params: DexParams | None = None,
        rpc_url: str | None = None,
    ):
        """
        Initialize Aerodrome adapter.

        Args:
            lp_private_key: Private key for LP position management
            trade_private_key: Private key for market-making swaps (defaults to lp_private_key)
            params: DEX strategy parameters (uses defaults if not provided)
            rpc_url: Override RPC URL (useful for testing with Anvil)
        """
        if params is None:
            params = DexParams()

        if trade_private_key is None:
            trade_private_key = lp_private_key

        # Create config with optional RPC override
        config = AERODROME_CNGN_USDC_CONFIG
        if rpc_url:
            # Create a new config with overridden RPC URL
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
            )

        super().__init__(
            pool_config=config,
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
