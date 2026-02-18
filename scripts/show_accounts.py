"""Print all derived wallet addresses and their funding requirements.

Usage:
    python scripts/show_accounts.py
"""

from engine.core.accounts import AccountManager, DEFAULT_ACCOUNT_CONFIGS

CHAIN_NAMES = {1: "Ethereum", 8453: "Base", 56: "BSC"}
NATIVE_SYMBOLS = {1: "ETH", 8453: "ETH", 56: "BNB"}


def main():
    mgr = AccountManager()

    print()
    print("=" * 64)
    print("  Derived wallet addresses")
    print("=" * 64)

    for role, config in DEFAULT_ACCOUNT_CONFIGS.items():
        address = mgr.get_address(role)
        chain = CHAIN_NAMES.get(config.chain_id, str(config.chain_id))
        native = NATIVE_SYMBOLS.get(config.chain_id, "ETH")

        print()
        print(f"  {role.value}")
        print(f"    Address : {address}")
        print(f"    Network : {chain} (chain {config.chain_id})")
        print(f"    Path    : {config.derivation_path}")
        print(f"    Needs   : {native} ≥ {config.min_balance_eth} (gas)")
        for token, minimum in config.min_balance_tokens.items():
            print(f"            + {token} ≥ {minimum}")

    print()
    print("=" * 64)
    print()


if __name__ == "__main__":
    main()
