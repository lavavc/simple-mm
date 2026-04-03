#!/usr/bin/env python3
"""Manually buy cNGN on a Uniswap V4 venue using stablecoin."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.config import settings
from engine.accounts import AccountManager, AccountRole
from engine.venues.dex.uniswap_base import UniswapBaseV4Adapter
from engine.venues.dex.uniswap_bsc import UniswapBscV4Adapter

_ACCOUNT_ROLES = {
    "uni-base-lp": AccountRole.UNI_BASE_LP,
    "uni-base-trade": AccountRole.UNI_BASE_TRADE,
    "uni-bsc-lp": AccountRole.UNI_BSC_LP,
    "uni-bsc-trade": AccountRole.UNI_BSC_TRADE,
}


def _build_adapter(account: str):
    role = _ACCOUNT_ROLES[account]
    account_manager = AccountManager(
        mnemonic=settings.wallet_mnemonic if settings.wallet_mnemonic else None,
        use_test_accounts=settings.use_test_accounts,
    )
    key = account_manager.get_private_key(role)

    if account.startswith("uni-base"):
        lp_key = account_manager.get_private_key(AccountRole.UNI_BASE_LP)
        return UniswapBaseV4Adapter(
            lp_private_key=lp_key,
            trade_private_key=key,
            rpc_url=settings.base_rpc_url,
            params=settings.uni_base_lp_params,
        )

    if account.startswith("uni-bsc"):
        lp_key = account_manager.get_private_key(AccountRole.UNI_BSC_LP)
        return UniswapBscV4Adapter(
            lp_private_key=lp_key,
            trade_private_key=key,
            rpc_url=settings.bsc_rpc_url,
            params=settings.uni_bsc_lp_params,
        )

    raise ValueError(f"Unsupported account: {account}")


async def _run(account: str, amount_usd: Decimal, min_out_cngn: Decimal, execute: bool):
    adapter = _build_adapter(account)

    stable_balance_raw = adapter.stable_token.functions.balanceOf(adapter.trade_account.address).call()
    stable_balance = Decimal(stable_balance_raw) / Decimal(10 ** adapter.stable_decimals)

    price_quote = await adapter.get_current_price()
    current_price = price_quote.mid if price_quote else Decimal("0")
    expected_cngn = (amount_usd / current_price) if current_price > 0 else Decimal("0")

    amount_in_raw = int(amount_usd * Decimal(10 ** adapter.stable_decimals))
    min_out_raw = int(min_out_cngn * Decimal(10 ** adapter.cngn_decimals))

    print(f"account={account}")
    print(f"address={adapter.trade_account.address}")
    print(f"stable_balance={stable_balance}")
    print(f"amount_usd={amount_usd}")
    print(f"min_out_cngn={min_out_cngn}")
    print(f"current_price={current_price}")
    print(f"expected_cngn={expected_cngn}")

    if amount_usd > stable_balance:
        raise ValueError(
            f"Requested buy amount {amount_usd} exceeds wallet stable balance {stable_balance}"
        )

    if not execute:
        print("check_only=true")
        return

    result = await adapter.swap(adapter.stable_address, amount_in_raw, min_out_raw)
    print(f"tx_hash={result.hash}")
    print(f"status={result.status}")
    print(f"gas_used={result.gas_used}")
    if result.output_raw is not None:
        output_cngn = Decimal(result.output_raw) / Decimal(10 ** adapter.cngn_decimals)
        print(f"output_cngn={output_cngn}")
    if result.error:
        print(f"error={result.error}")


def main():
    parser = argparse.ArgumentParser(description="Manually buy cNGN on a V4 venue.")
    parser.add_argument("--account", choices=list(_ACCOUNT_ROLES), required=True)
    parser.add_argument("--amount-usd", type=Decimal, required=True)
    parser.add_argument("--min-out-cngn", type=Decimal, default=Decimal("0"))
    parser.add_argument("--execute", action="store_true", help="Actually send the transaction")
    args = parser.parse_args()

    asyncio.run(
        _run(
            account=args.account,
            amount_usd=args.amount_usd,
            min_out_cngn=args.min_out_cngn,
            execute=args.execute,
        )
    )


if __name__ == "__main__":
    main()
