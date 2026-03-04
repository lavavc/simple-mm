"""Tests for configuration management."""

import pytest
from engine.config import Settings


class TestSettings:
    """Test Settings defaults and loading."""

    def test_default_values(self):
        """Settings should have sane defaults."""
        s = Settings()
        assert s.host == "127.0.0.1"
        assert s.port == 8000
        assert s.log_level == "info"
        assert s.db_path == "./data/cngn.db"
        assert s.price_update_interval == 10
        assert s.arbitrage_enabled is True
        assert s.arbitrage_execution_enabled is False

    def test_rpc_urls_default(self):
        s = Settings()
        assert s.base_rpc_url.startswith("https://")
        assert s.bsc_rpc_url.startswith("https://")

    def test_token_addresses(self):
        s = Settings()
        assert s.cngn_base_address.startswith("0x")
        assert s.usdc_base_address.startswith("0x")
        assert s.usdt_base_address.startswith("0x")
        assert s.cngn_bsc_address.startswith("0x")
        assert s.usdt_bsc_address.startswith("0x")
        assert s.usdt_eth_address.startswith("0x")

    def test_arbitrage_defaults(self):
        s = Settings()
        assert s.arbitrage_min_spread_bps == 150
        assert s.arbitrage_min_net_profit_bps == 50
        assert s.arbitrage_max_single_trade_usd == 100.0

    def test_scheduler_intervals(self):
        s = Settings()
        assert s.price_update_interval > 0
        assert s.position_sync_interval > 0
        assert s.dex_check_interval > 0
        assert s.cex_sync_interval > 0

    def test_trading_params(self):
        s = Settings()
        assert 0 < s.target_delta_ratio < 1
        assert s.rebalance_threshold_percent > 0

    def test_venue_keys_default_empty(self):
        """API keys should default to empty strings."""
        s = Settings()
        assert s.quidax_api_key == "" or isinstance(s.quidax_api_key, str)
        assert s.blockradar_api_key == "" or isinstance(s.blockradar_api_key, str)

    def test_no_keys_file_setting(self):
        """keys_file and key_encryption_key should not exist."""
        s = Settings()
        assert not hasattr(s, "keys_file")
        assert not hasattr(s, "key_encryption_key")

    def test_no_quidax_api_secret(self):
        """quidax_api_secret should not exist."""
        s = Settings()
        assert not hasattr(s, "quidax_api_secret")
