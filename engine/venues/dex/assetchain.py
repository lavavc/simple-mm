"""AssetChain V3 DEX adapter (Uniswap V3 fork)."""

from .base import BaseDexAdapter, PoolConfig, PoolReadConfig
from .abis import POOL_ABI, NFT_POSITION_MANAGER_ABI, ROUTER_ABI
from engine.api.schemas import DexParams
from engine.config import settings

# ── Token addresses on AssetChain ───────────────────────────────────────
CNGN_ASSETCHAIN = "0x7923C0f6FA3d1BA6EAFCAedAaD93e737Fd22FC4F"
USDT_ASSETCHAIN = "0x26E490d30e73c36800788DC6d6315946C4BbEa24"

# ── AssetChain V3 infrastructure ────────────────────────────────────────
# Dummy addresses for router and NFT manager until live execution is enabled
ASSETCHAIN_NFT_MANAGER = "0x0000000000000000000000000000000000000000"
ASSETCHAIN_ROUTER = "0x0000000000000000000000000000000000000000"

# ── Pool configuration ──────────────────────────────────────────────────
# On-chain token ordering: token0=USDT(0x26E4...) < token1=cNGN(0x7923...)
ASSETCHAIN_CNGN_USDT_CONFIG = PoolConfig(
    chain_id=42420,
    chain_name="assetchain",
    rpc_url=settings.assetchain_rpc_url,
    pool_address="0xE2a45a102B00Fad6447d0AD859b43BAf8bF6DeF1",
    nft_manager_address=ASSETCHAIN_NFT_MANAGER,
    router_address=ASSETCHAIN_ROUTER,
    token0_address=USDT_ASSETCHAIN,  # USDT is token0
    token1_address=CNGN_ASSETCHAIN,  # cNGN is token1
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    tick_spacing=60,
    pool_fee=3000,
    invert_price=True,  # Native price is cNGN/USDT; we want USDT/cNGN
)

# ── Read-only config for price fetching (no keys needed) ────────────────
ASSETCHAIN_POOL_READ_CONFIG = PoolReadConfig(
    rpc_url=settings.assetchain_rpc_url,
    pool_address="0xE2a45a102B00Fad6447d0AD859b43BAf8bF6DeF1",
    token0_address=USDT_ASSETCHAIN,
    token1_address=CNGN_ASSETCHAIN,
    token0_symbol="USDT",
    token1_symbol="cNGN",
    token0_decimals=18,
    token1_decimals=6,
    invert_price=True,
)

class AssetChainAdapter(BaseDexAdapter):
    """AssetChain V3 DEX adapter."""

    name = "assetchain"

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

        config = ASSETCHAIN_CNGN_USDT_CONFIG
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
        return POOL_ABI

    def get_nft_manager_abi(self) -> list:
        return NFT_POSITION_MANAGER_ABI

    def get_router_abi(self) -> list:
        return ROUTER_ABI
