"""Configuration management using Pydantic Settings."""

from decimal import Decimal

from pydantic_settings import BaseSettings
from pydantic import Field, model_validator
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    # Database
    db_path: str = "./data/cngn.db"

    # Dashboard auth
    dashboard_api_token: str = Field(default="", description="Token for dashboard auth")

    # RPC endpoints — set ALCHEMY_KEY to use Alchemy for all chains,
    # or override individual URLs directly.
    alchemy_key: str = Field(default="", description="Alchemy API key (used for all chain RPCs)")
    base_rpc_url: str = "https://mainnet.base.org"
    bsc_rpc_url: str = "https://bsc-dataseed.binance.org"
    eth_rpc_url: str = "https://eth.llamarpc.com"
    assetchain_rpc_url: str = "https://mainnet-rpc.assetchain.org"
    
    # Websocket endpoints
    base_wss_url: str = ""
    bsc_wss_url: str = ""

    @model_validator(mode="after")
    def apply_alchemy_key(self) -> "Settings":
        if self.alchemy_key:
            self.base_rpc_url = f"https://base-mainnet.g.alchemy.com/v2/{self.alchemy_key}"
            self.bsc_rpc_url = f"https://bnb-mainnet.g.alchemy.com/v2/{self.alchemy_key}"
            self.eth_rpc_url = f"https://eth-mainnet.g.alchemy.com/v2/{self.alchemy_key}"
            # Injecting WSS endpoints for the Event Listener
            self.base_wss_url = f"wss://base-mainnet.g.alchemy.com/v2/{self.alchemy_key}"
            self.bsc_wss_url = f"wss://bnb-mainnet.g.alchemy.com/v2/{self.alchemy_key}"
        return self

    # Venue API keys
    quidax_api_key: str = Field(default="", description="Quidax arb account secret key (Bearer token)")
    quidax_lp_api_key: str = Field(default="", description="Quidax LP account secret key (Bearer token)")
    quidax_deposit_address: str = Field(default="", description="Quidax static deposit address")
    blockradar_api_key: str = Field(default="", description="Blockradar API key")
    blockradar_wallet_id: str = Field(default="", description="Blockradar wallet ID for swaps")
    blockradar_deposit_address: str = Field(default="", description="Blockradar on-chain deposit address")

    # Optional notifications
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # Scheduler intervals (seconds)
    price_update_interval: int = 10
    position_sync_interval: int = 60
    dex_check_interval: int = 120
    cex_sync_interval: int = 300

    # Trading parameters
    target_delta_ratio: float = 0.5
    rebalance_threshold_percent: float = 5.0
    delta_alert_threshold_percent: float = 10.0  # Alert if delta deviates >10% from target
    portfolio_delta_interval: int = 120  # Check portfolio delta every 2 minutes
    venue_divergence_rebalance_bps: int = 200  # Rebalance DEX if venue drifts >2% from fair value

    # Arbitrage settings
    # Global arbitrage toggle (must be True for both detection and execution)
    arbitrage_enabled: bool = True
    
    # ----------------------------------------------------
    # Arbitrage Engine Defaults
    # Controls if detected opportunities will actually trigger transactions
    arbitrage_execution_enabled: bool = False
    arbitrage_scan_interval: int = 10  # seconds

    # Arbitrage thresholds — all ArbitrageParams defaults live here, nowhere else
    arbitrage_min_spread_bps: int = 150          # 1.5% minimum gross spread
    arbitrage_min_net_profit_bps: int = 50       # 0.5% minimum after fees
    arbitrage_dex_swap_fee_bps: int = 30         # Fallback if on-chain fee() call fails
    arbitrage_cex_taker_fee_bps: int = 25        # CEX taker fee
    arbitrage_max_single_trade_usd: float = 100.0          # Fallback when pool reserves unavailable
    arbitrage_max_daily_volume_usd: float = 10000.0
    arbitrage_max_inventory_imbalance_usd: float = 5000.0
    arbitrage_max_consecutive_failures: int = 3
    arbitrage_max_daily_loss_usd: float = 500.0
    arbitrage_cross_chain_rebalance_bps: int = 10
    arbitrage_max_delta_ratio: float = 0.60
    arbitrage_min_account_stablecoin_usd: float = 10.0

    # Quidax auto-funding thresholds
    quidax_min_cngn: Decimal = Field(default=Decimal("10000"))
    quidax_top_up_cngn: Decimal = Field(default=Decimal("20000"))
    quidax_min_usdt: Decimal = Field(default=Decimal("10"))
    quidax_top_up_usdt: Decimal = Field(default=Decimal("50"))
    quidax_onchain_min_cngn: Decimal = Field(default=Decimal("10000"))
    quidax_onchain_min_usdt: Decimal = Field(default=Decimal("10"))

    # Account management
    use_test_accounts: bool = False  # Use Anvil test mnemonic (for local dev)
    wallet_mnemonic: str = Field(default="", description="BIP39 mnemonic for HD wallet derivation")
    balance_check_interval: int = 300  # Check balances every 5 minutes

    # Token contract addresses
    cngn_base_address: str = "0x46C85152bFe9f96829aA94755D9f915F9B10EF5F"
    usdc_base_address: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    usdt_base_address: str = "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2"
    cngn_bsc_address: str = "0xa8aea66b361a8d53e8865c62d142167af28af058"
    usdt_bsc_address: str = "0x55d398326f99059fF775485246999027B3197955"
    usdt_eth_address: str = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    cngn_assetchain_address: str = "0x7923C0f6FA3d1BA6EAFCAedAaD93e737Fd22FC4F"
    usdt_assetchain_address: str = "0x26E490d30e73c36800788DC6d6315946C4BbEa24"

    # Uniswap V4 contract addresses
    uni_bsc_pool_manager: str = "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df"
    uni_base_pool_manager: str = "0x498581ff718922c3f8e6a244956af099b2652b2b"
    uni_bsc_state_view: str = "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4"
    uni_base_state_view: str = "0xa3c0c9b65bad0b08107aa264b0f3db444b867a71"
    uni_bsc_pool_id: str = "0x2268f03a28f37f16cd3610dc669536f8c815d9d4cb2906feeeba9150fb2d8596"
    uni_base_pool_id: str = "0x84fa97768196067f0e5aa157709039a3897e219cba3002d9ad38bf44e300fe93"

    # Aerodrome contract addresses (Base)
    aerodrome_pool_address: str = "0x0206B696a410277eF692024C2B64CcF4EaC78589"
    aerodrome_nft_manager_address: str = "0x827922686190790b37229fd06084350E74485b72"
    aerodrome_router_address: str = "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"

    # PancakeSwap contract addresses (BSC)
    pancakeswap_pool_address: str = "0xb84e7c912a1034ad674bba8859fca84f1f614a29"
    pancakeswap_nft_manager_address: str = "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"
    pancakeswap_router_address: str = "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4"

    # AssetChain contract addresses
    assetchain_pool_address: str = "0xE2a45a102B00Fad6447d0AD859b43BAf8bF6DeF1"
    assetchain_nft_manager_address: str = "0x0000000000000000000000000000000000000000"
    assetchain_router_address: str = "0x0000000000000000000000000000000000000000"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
