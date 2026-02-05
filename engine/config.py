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

    # Security
    key_encryption_key: str = Field(default="", description="32+ char secret for key encryption")
    dashboard_api_token: str = Field(default="", description="Token for dashboard auth")

    # RPC endpoints
    base_rpc_url: str = "https://mainnet.base.org"
    bsc_rpc_url: str = "https://bsc-dataseed.binance.org"

    # Keys file
    keys_file: str = "./keys.encrypted.json"

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

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
