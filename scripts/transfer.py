"""Transfer ERC20 tokens or native gas token from an engine account to any address.

Usage:
    python scripts/transfer.py --role uni-bsc-trade --token USDT --to 0x... --amount 50
    python scripts/transfer.py --role uni-bsc-trade --token BNB --to 0x... --amount 0.01

Roles: uni-base-lp, uni-base-trade, blockradar, quidax-trade-fund, quidax-lp,
       uni-bsc-lp, uni-bsc-trade
Tokens by chain:
  Base (8453) : CNGN, USDC, USDT, ETH
  BSC  (56)   : CNGN, USDT, BNB
"""

import argparse
import asyncio
from decimal import Decimal

from engine.config import settings
from engine.core.accounts import AccountManager, AccountRole

TOKEN_ADDRESSES = {
    ("CNGN", 8453): settings.cngn_base_address,
    ("USDC", 8453): settings.usdc_base_address,
    ("USDT", 8453): settings.usdt_base_address,
    ("CNGN", 56): settings.cngn_bsc_address,
    ("USDT", 56): settings.usdt_bsc_address,
}


NATIVE_SYMBOLS = {
    8453: "ETH",
    56: "BNB",
}


async def main():
    parser = argparse.ArgumentParser(
        description="Transfer ERC20 or native token from an engine account"
    )
    parser.add_argument("--role", required=True, help="Account role")
    parser.add_argument(
        "--token",
        required=True,
        help="Token symbol (CNGN, USDT, USDC, BNB, ETH)",
    )
    parser.add_argument("--to", required=True, dest="to_address", help="Destination address")
    parser.add_argument("--amount", required=True, type=Decimal, help="Amount to transfer")
    args = parser.parse_args()

    role = AccountRole(args.role)
    mgr = AccountManager()

    chain_id = mgr.get_config(role).chain_id
    token_symbol = args.token.upper()
    native_symbol = NATIVE_SYMBOLS.get(chain_id, "ETH")
    is_native = token_symbol in {"NATIVE", native_symbol}

    if not is_native:
        token_key = (token_symbol, chain_id)
        if token_key not in TOKEN_ADDRESSES:
            available = [k[0] for k in TOKEN_ADDRESSES if k[1] == chain_id] + [native_symbol]
            raise SystemExit(
                f"Unknown token {token_symbol!r} on chain {chain_id}. Available: {available}"
            )
        token_address = TOKEN_ADDRESSES[token_key]

    from_address = mgr.get_address(role)

    print(f"\nTransfer {args.amount} {token_symbol}")
    print(f"  From : {from_address} ({role.value})")
    print(f"  To   : {args.to_address}")
    if is_native:
        print(f"  Type : Native ({native_symbol})\n")
    else:
        print(f"  Token: {token_address}\n")

    confirm = input("Confirm? [y/N] ")
    if confirm.strip().lower() != "y":
        raise SystemExit("Aborted.")

    if is_native:
        tx_hash = await mgr.transfer_native(role, args.to_address, args.amount)
    else:
        tx_hash = await mgr.transfer_erc20(role, token_address, args.to_address, args.amount)
    print(f"Sent: {tx_hash}")


if __name__ == "__main__":
    asyncio.run(main())
