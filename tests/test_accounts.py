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


def test_deterministic_derivation():
    """Same mnemonic always produces the same address for each role."""
    mgr1 = AccountManager(use_test_accounts=True)
    mgr2 = AccountManager(use_test_accounts=True)
    for role in AccountRole:
        assert mgr1.get_address(role) == mgr2.get_address(role)


def test_missing_mnemonic_raises_not_silently_derives():
    """No mnemonic available → ValueError, not a silent derivation from empty seed."""
    old = os.environ.pop("WALLET_MNEMONIC", None)
    try:
        with pytest.raises(ValueError, match="No mnemonic"):
            AccountManager()
    finally:
        if old is not None:
            os.environ["WALLET_MNEMONIC"] = old
