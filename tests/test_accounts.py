"""Security-critical account derivation invariants.

These are not tests of the HD wallet library — they pin our specific
configuration: that no two roles share a derivation path, that addresses
are deterministic, and that a missing mnemonic fails loudly rather than
silently deriving from an empty seed.
"""

import os
import pytest

from engine.accounts import (
    AccountManager,
    AccountRole,
    ANVIL_TEST_MNEMONIC,
    DEFAULT_ACCOUNT_CONFIGS,
)


def test_unique_derivation_paths():
    """No two account roles may share an HD derivation path."""
    paths = [c.derivation_path for c in DEFAULT_ACCOUNT_CONFIGS.values()]
    assert len(paths) == len(set(paths)), "Duplicate derivation paths would produce identical keys"


def test_derived_addresses_are_different():
    """Every role must derive a distinct on-chain address."""
    mgr = AccountManager(use_test_accounts=True)
    addresses = list(mgr.list_accounts().values())
    assert len(addresses) == len(set(addresses)), "Roles must not share an address"


def test_missing_mnemonic_raises_not_silently_derives():
    """No mnemonic available → ValueError, not a silent derivation from empty seed."""
    old = os.environ.pop("WALLET_MNEMONIC", None)
    try:
        with pytest.raises(ValueError, match="No mnemonic"):
            AccountManager()
    finally:
        if old is not None:
            os.environ["WALLET_MNEMONIC"] = old


@pytest.mark.asyncio
async def test_monitoring_reads_share_cache_but_direct_balance_reads_stay_fresh():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    mgr = AccountManager(use_test_accounts=True)
    mgr._accounts = {AccountRole.UNI_BASE_TRADE: mgr.get_account(AccountRole.UNI_BASE_TRADE)}
    first = SimpleNamespace(role="uni-base-trade", token_balances={"USDC": 2})
    second = SimpleNamespace(role="uni-base-trade", token_balances={"USDC": 3})
    mgr.get_balance = AsyncMock(return_value=first)
    contracts = {8453: {"USDC": "token"}}
    a, b = await asyncio.gather(mgr.check_all_balances(contracts), mgr.check_all_balances(contracts))
    assert a == b == [first]
    mgr.get_balance.assert_awaited_once()

    mgr.get_balance.return_value = second
    assert await mgr.get_balance(AccountRole.UNI_BASE_TRADE, contracts) is second
    assert await mgr.check_all_balances(contracts) == [first]
    mgr._balance_snapshot_at -= 11
    assert await mgr.check_all_balances(contracts) == [second]
    assert mgr.get_balance.await_count == 3

    await mgr.check_all_balances({8453: {"USDC": "other-token"}})
    assert mgr.get_balance.await_count == 4
