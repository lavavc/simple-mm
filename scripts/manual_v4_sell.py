#!/usr/bin/env python3
"""Manually sell cNGN on a Uniswap V4 venue to unwind a half-open position."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.api.schemas import DexParams
from engine.config import settings
from engine.core.accounts import AccountManager, AccountRole
from engine.venues.dex.uniswap_base import UniswapBaseV4Adapter
from engine.venues.dex.uniswap_bsc import UniswapBscV4Adapter


def _build_adapter(venue: str):
    account_manager = AccountManager(
        mnemonic=settings.wallet_mnemonic if settings.wallet_mnemonic else None,
        use_test_accounts=settings.use_test_accounts,
    )

    if venue == "uni-base":
        lp_key = account_manager.get_private_key(AccountRole.UNI_BASE_LP)
        trade_key = account_manager.get_private_key(AccountRole.UNI_BASE_TRADE)
        return UniswapBaseV4Adapter(
            lp_private_key=lp_key,
            trade_private_key=trade_key,
            rpc_url=settings.base_rpc_url,
            params=DexParams(),
        )

    if venue == "uni-bsc":
        lp_key = account_manager.get_private_key(AccountRole.UNI_BSC_LP)
        trade_key = account_manager.get_private_key(AccountRole.UNI_BSC_TRADE)
        return UniswapBscV4Adapter(
            lp_private_key=lp_key,
            trade_private_key=trade_key,
            rpc_url=settings.bsc_rpc_url,
            params=DexParams(),
        )

    raise ValueError(f"Unsupported venue: {venue}")


async def _run(venue: str, amount_cngn: Decimal, min_out_stable: Decimal, execute: bool):
    adapter = _build_adapter(venue)
    balance_raw = adapter.w3.eth.contract(
        address=adapter.w3.to_checksum_address(adapter.cngn_address),
        abi=adapter.token0.abi,
    ).functions.balanceOf(adapter.trade_account.address).call()
    balance_cngn = Decimal(balance_raw) / Decimal(10 ** adapter.cngn_decimals)

    amount_in_raw = int(amount_cngn * Decimal(10 ** adapter.cngn_decimals))
    min_out_raw = int(min_out_stable * Decimal(10 ** adapter.stable_decimals))

    print(f"venue={venue}")
    print(f"trade_account={adapter.trade_account.address}")
    print(f"cngn_balance={balance_cngn}")
    print(f"amount_cngn={amount_cngn}")
    print(f"min_out_stable={min_out_stable}")

    if amount_cngn > balance_cngn:
        raise ValueError(
            f"Requested sell amount {amount_cngn} exceeds wallet balance {balance_cngn}"
        )

    if not execute:
        print("check_only=true")
        return

    result = await adapter.swap(adapter.cngn_address, amount_in_raw, min_out_raw)
    print(f"tx_hash={result.hash}")
    print(f"status={result.status}")
    print(f"gas_used={result.gas_used}")
    if result.error:
        print(f"error={result.error}")


def main():
    parser = argparse.ArgumentParser(description="Manually sell cNGN on a V4 venue.")
    parser.add_argument("--venue", choices=["uni-base", "uni-bsc"], required=True)
    parser.add_argument("--amount-cngn", type=Decimal, required=True)
    parser.add_argument("--min-out-stable", type=Decimal, default=Decimal("0"))
    parser.add_argument("--execute", action="store_true", help="Actually send the transaction")
    args = parser.parse_args()

    asyncio.run(
        _run(
            venue=args.venue,
            amount_cngn=args.amount_cngn,
            min_out_stable=args.min_out_stable,
            execute=args.execute,
        )
    )


if __name__ == "__main__":
    main()
