"""Transfer ERC20 tokens from an engine account to any address.

Usage:
    python scripts/transfer.py --role pancakeswap-trade --token USDT --to 0x... --amount 50

Roles: aerodrome-lp, aerodrome-trade, blockradar, quidax-arb, quidax-lp,
       pancakeswap-lp, pancakeswap-trade
Tokens by chain:
  Base (8453) : cNGN, USDC, USDT
  BSC  (56)   : cNGN, USDT
"""

import argparse
import asyncio
from decimal import Decimal

from engine.config import settings
from engine.core.accounts import AccountManager, AccountRole

TOKEN_ADDRESSES = {
    ("cNGN", 8453): settings.cngn_base_address,
    ("USDC", 8453): settings.usdc_base_address,
    ("USDT", 8453): settings.usdt_base_address,
    ("cNGN", 56): settings.cngn_bsc_address,
    ("USDT", 56): settings.usdt_bsc_address,
}


async def main():
    parser = argparse.ArgumentParser(description="Transfer ERC20 from an engine account")
    parser.add_argument("--role", required=True, help="Account role")
    parser.add_argument("--token", required=True, help="Token symbol (cNGN, USDT, USDC)")
    parser.add_argument("--to", required=True, dest="to_address", help="Destination address")
    parser.add_argument("--amount", required=True, type=Decimal, help="Amount to transfer")
    args = parser.parse_args()

    role = AccountRole(args.role)
    mgr = AccountManager()

    chain_id = mgr.get_config(role).chain_id
    token_key = (args.token, chain_id)
    if token_key not in TOKEN_ADDRESSES:
        raise SystemExit(f"Unknown token {args.token!r} on chain {chain_id}. Available: {[k for k in TOKEN_ADDRESSES if k[1] == chain_id]}")

    token_address = TOKEN_ADDRESSES[token_key]
    from_address = mgr.get_address(role)

    print(f"\nTransfer {args.amount} {args.token}")
    print(f"  From : {from_address} ({role.value})")
    print(f"  To   : {args.to_address}")
    print(f"  Token: {token_address}\n")

    confirm = input("Confirm? [y/N] ")
    if confirm.strip().lower() != "y":
        raise SystemExit("Aborted.")

    tx_hash = await mgr.transfer_erc20(role, token_address, args.to_address, args.amount)
    print(f"Sent: {tx_hash}")


if __name__ == "__main__":
    asyncio.run(main())
