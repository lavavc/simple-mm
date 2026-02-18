"""Tests for HD wallet account management."""

import copy
import pytest
from decimal import Decimal

from engine.core.accounts import (
    AccountManager,
    AccountRole,
    AccountConfig,
    ANVIL_TEST_MNEMONIC,
    DEFAULT_ACCOUNT_CONFIGS,
)


def _fresh_configs():
    """Return a deep copy of default configs to avoid cross-test mutation."""
    return copy.deepcopy(DEFAULT_ACCOUNT_CONFIGS)


# =============================================================================
# AccountManager initialization
# =============================================================================


class TestAccountManagerInit:
    """Test account manager initialization."""

    def test_init_with_test_mnemonic(self):
        """Should derive accounts using Anvil test mnemonic."""
        mgr = AccountManager(use_test_accounts=True)
        accounts = mgr.list_accounts()

        assert len(accounts) == 6
        assert "aerodrome-lp" in accounts
        assert "aerodrome-trade" in accounts
        assert "blockradar" in accounts
        assert "quidax" in accounts
        assert "pancakeswap-lp" in accounts
        assert "pancakeswap-trade" in accounts

    def test_init_with_explicit_mnemonic(self):
        """Should derive accounts from provided mnemonic."""
        mgr = AccountManager(mnemonic=ANVIL_TEST_MNEMONIC)
        accounts = mgr.list_accounts()
        assert len(accounts) == 6

    def test_init_without_mnemonic_raises(self):
        """Should raise ValueError if no mnemonic is available."""
        import os
        old = os.environ.pop("WALLET_MNEMONIC", None)
        try:
            with pytest.raises(ValueError, match="No mnemonic"):
                AccountManager()
        finally:
            if old is not None:
                os.environ["WALLET_MNEMONIC"] = old

    def test_derived_addresses_are_different(self):
        """Each role should get a unique address."""
        mgr = AccountManager(use_test_accounts=True)
        addresses = list(mgr.list_accounts().values())
        assert len(addresses) == len(set(addresses)), "Addresses must be unique"

    def test_deterministic_derivation(self):
        """Same mnemonic should produce same addresses."""
        mgr1 = AccountManager(use_test_accounts=True)
        mgr2 = AccountManager(use_test_accounts=True)

        for role in AccountRole:
            assert mgr1.get_address(role) == mgr2.get_address(role)


# =============================================================================
# Account access
# =============================================================================


class TestAccountAccess:
    """Test account retrieval methods."""

    @pytest.fixture
    def mgr(self):
        return AccountManager(use_test_accounts=True, account_configs=_fresh_configs())

    def test_get_account(self, mgr):
        account = mgr.get_account(AccountRole.AERODROME_LP)
        assert account.address.startswith("0x")
        assert len(account.address) == 42

    def test_get_private_key(self, mgr):
        key = mgr.get_private_key(AccountRole.AERODROME_LP)
        assert isinstance(key, str)
        assert len(key) > 0

    def test_get_address(self, mgr):
        addr = mgr.get_address(AccountRole.AERODROME_LP)
        assert addr.startswith("0x")
        assert len(addr) == 42

    def test_get_config(self, mgr):
        config = mgr.get_config(AccountRole.AERODROME_LP)
        assert config.role == AccountRole.AERODROME_LP
        assert config.chain_id == 8453
        assert "cNGN" in config.tokens

    def test_get_invalid_account_raises(self, mgr):
        """Non-existent role should raise ValueError."""
        # Copy accounts dict and remove one to avoid mutating shared state
        saved = mgr._accounts.copy()
        del mgr._accounts[AccountRole.QUIDAX]
        try:
            with pytest.raises(ValueError, match="No account configured"):
                mgr.get_account(AccountRole.QUIDAX)
        finally:
            mgr._accounts = saved


# =============================================================================
# Threshold updates
# =============================================================================


class TestThresholdUpdates:
    """Test refill threshold management."""

    @pytest.fixture
    def mgr(self):
        return AccountManager(use_test_accounts=True, account_configs=_fresh_configs())

    def test_update_eth_threshold(self, mgr):
        mgr.update_thresholds(AccountRole.AERODROME_LP, min_balance_eth=Decimal("0.05"))
        config = mgr.get_config(AccountRole.AERODROME_LP)
        assert config.min_balance_eth == Decimal("0.05")

    def test_update_token_thresholds(self, mgr):
        mgr.update_thresholds(
            AccountRole.AERODROME_LP,
            min_balance_tokens={"cNGN": Decimal("100000")},
        )
        config = mgr.get_config(AccountRole.AERODROME_LP)
        assert config.min_balance_tokens["cNGN"] == Decimal("100000")

    def test_update_preserves_other_thresholds(self, mgr):
        original_eth = mgr.get_config(AccountRole.AERODROME_LP).min_balance_eth
        mgr.update_thresholds(
            AccountRole.AERODROME_LP,
            min_balance_tokens={"cNGN": Decimal("99999")},
        )
        assert mgr.get_config(AccountRole.AERODROME_LP).min_balance_eth == original_eth

    def test_update_invalid_role_raises(self, mgr):
        saved = mgr._configs.copy()
        del mgr._configs[AccountRole.QUIDAX]
        try:
            with pytest.raises(ValueError, match="No config for role"):
                mgr.update_thresholds(AccountRole.QUIDAX, min_balance_eth=Decimal("1"))
        finally:
            mgr._configs = saved


# =============================================================================
# Default configs
# =============================================================================


class TestDefaultConfigs:
    """Test default account configurations."""

    def test_all_roles_configured(self):
        for role in AccountRole:
            assert role in DEFAULT_ACCOUNT_CONFIGS

    def test_unique_derivation_paths(self):
        paths = [c.derivation_path for c in DEFAULT_ACCOUNT_CONFIGS.values()]
        assert len(paths) == len(set(paths)), "Derivation paths must be unique"

    def test_aerodrome_lp_config(self):
        config = DEFAULT_ACCOUNT_CONFIGS[AccountRole.AERODROME_LP]
        assert config.chain_id == 8453
        assert "cNGN" in config.tokens
        assert "USDC" in config.tokens

    def test_blockradar_config(self):
        config = DEFAULT_ACCOUNT_CONFIGS[AccountRole.BLOCKRADAR]
        assert "cNGN" in config.tokens
        assert "USDT" in config.tokens
        assert "USDC" in config.tokens
