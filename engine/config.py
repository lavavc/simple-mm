"""Configuration management using Pydantic Settings."""

from pydantic_settings import BaseSettings
from pydantic import Field
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

    # RPC endpoints
    base_rpc_url: str = "https://mainnet.base.org"
    bsc_rpc_url: str = "https://bsc-dataseed.binance.org"

    # Venue API keys
    quidax_api_key: str = Field(default="", description="Quidax secret key (Bearer token)")
    blockradar_api_key: str = Field(default="", description="Blockradar API key")

    # Optional notifications
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # Scheduler intervals (seconds)
    price_update_interval: int = 30
    position_sync_interval: int = 60
    dex_check_interval: int = 120
    cex_sync_interval: int = 300
    rate_sync_interval: int = 300
    rebalance_check_interval: int = 120

    # Trading parameters
    target_delta_ratio: float = 0.5
    rebalance_threshold_percent: float = 5.0

    # Arbitrage settings
    arbitrage_enabled: bool = True
    arbitrage_execution_enabled: bool = False  # Phase 1: detection only
    arbitrage_scan_interval: int = 30  # seconds

    # Arbitrage thresholds (can be overridden via API)
    arbitrage_min_spread_bps: int = 150  # 1.5% minimum gross spread
    arbitrage_min_net_profit_bps: int = 50  # 0.5% minimum after fees
    arbitrage_max_single_trade_usd: float = 1000.0
    arbitrage_max_daily_volume_usd: float = 10000.0
    arbitrage_max_inventory_imbalance_usd: float = 5000.0

    # Account management
    use_test_accounts: bool = False  # Use Anvil test mnemonic (for local dev)
    wallet_mnemonic: str = Field(default="", description="BIP39 mnemonic for HD wallet derivation")
    balance_check_interval: int = 300  # Check balances every 5 minutes

    # Token contract addresses (Base chain)
    cngn_contract_address: str = "0x46C85152bFe9f96829aA94755D9f915F9B10EF5F"
    usdc_contract_address: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    usdt_contract_address: str = "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2"  # Base USDT

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
