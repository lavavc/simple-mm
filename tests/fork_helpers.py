"""Helpers for Anvil fork tests — wallet funding, donor finding, impersonation.

Extracted from scripts/simulate_lp_flow.py so the fork test (test_dex_fork.py
Section C) and the developer script share the same infrastructure without
maintaining two copies.

All helpers are synchronous (Anvil JSON-RPC calls are fire-and-forget);
`_seed_prices` is the only async function (it writes to an aiosqlite store).
"""

from __future__ import annotations

import contextlib
import time
from decimal import Decimal
from typing import Any, Generator

from web3 import Web3

from engine.db.repository import DatabaseRepository
from engine.types import PriceQuote

_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
_SINK_ADDRESS = "0x000000000000000000000000000000000000dEaD"


# =============================================================================
# Native / ERC20 funding
# =============================================================================


def fund_native_balance(w3: Web3, address: str, amount_eth: Decimal) -> None:
    """Set the native ETH balance of `address` via anvil_setBalance."""
    amount_wei = int(amount_eth * Decimal(10**18))
    response = w3.provider.make_request("anvil_setBalance", [address, hex(amount_wei)])
    if response.get("error"):
        raise RuntimeError(f"anvil_setBalance failed: {response['error']}")


def _topic_to_address(topic: Any) -> str:
    topic_hex = Web3.to_hex(topic)
    return Web3.to_checksum_address("0x" + topic_hex[-40:])


def _build_unlocked_tx_params(w3: Web3, from_address: str) -> dict[str, Any]:
    checksum = Web3.to_checksum_address(from_address)
    block = w3.eth.get_block("latest")
    base_fee = int(block.get("baseFeePerGas", w3.eth.gas_price))
    priority_fee = int(w3.to_wei(Decimal("0.1"), "gwei"))
    return {
        "from": checksum,
        "nonce": w3.eth.get_transaction_count(checksum, "pending"),
        "chainId": w3.eth.chain_id,
        "maxFeePerGas": (2 * base_fee) + priority_fee,
        "maxPriorityFeePerGas": priority_fee,
        "value": 0,
    }


@contextlib.contextmanager
def impersonated_account(w3: Web3, address: str) -> Generator[str, None, None]:
    """Context manager: impersonate an Anvil account via anvil_impersonateAccount."""
    checksum = Web3.to_checksum_address(address)
    response = w3.provider.make_request("anvil_impersonateAccount", [checksum])
    if response.get("error"):
        raise RuntimeError(f"anvil_impersonateAccount failed for {checksum}: {response['error']}")
    try:
        yield checksum
    finally:
        w3.provider.make_request("anvil_stopImpersonatingAccount", [checksum])


def transfer_erc20_from_unlocked(
    w3: Web3,
    token_contract: Any,
    *,
    sender: str,
    recipient: str,
    amount_raw: int,
) -> str:
    """Transfer `amount_raw` tokens from an impersonated `sender` to `recipient`.

    Funds the sender with 1 ETH for gas before sending.
    Returns the transaction hash as a hex string.
    """
    if amount_raw <= 0:
        return ""

    checksum_sender = Web3.to_checksum_address(sender)
    fund_native_balance(w3, checksum_sender, Decimal("1"))
    tx_params = _build_unlocked_tx_params(w3, checksum_sender)
    tx = token_contract.functions.transfer(
        Web3.to_checksum_address(recipient),
        int(amount_raw),
    ).build_transaction(tx_params)
    estimated = w3.eth.estimate_gas(
        {"from": tx["from"], "to": tx["to"], "data": tx["data"], "value": tx.get("value", 0)}
    )
    tx["gas"] = int(estimated * 1.2)
    tx_hash = w3.eth.send_transaction(tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"ERC20 transfer from impersonated {checksum_sender} failed")
    return Web3.to_hex(tx_hash)


def find_token_donor(
    w3: Web3,
    token_contract: Any,
    *,
    min_balance_raw: int,
    exclude: set[str],
    lookback_blocks: int = 2_000,
    batch_size: int = 50,
    max_candidates: int = 300,
) -> str:
    """Scan recent Transfer events to find a holder with at least `min_balance_raw` tokens.

    Returns the checksummed address of the first qualifying donor found.
    Raises RuntimeError if no donor is found within the lookback window.
    """
    latest = w3.eth.block_number
    floor = max(latest - lookback_blocks, 0)
    seen = {Web3.to_checksum_address(addr).lower() for addr in exclude}
    checked = 0

    for end_block in range(latest, floor - 1, -batch_size):
        start_block = max(floor, end_block - batch_size + 1)
        logs = w3.eth.get_logs(
            {
                "address": Web3.to_checksum_address(token_contract.address),
                "fromBlock": start_block,
                "toBlock": end_block,
                "topics": [_TRANSFER_TOPIC],
            }
        )
        for log in reversed(logs):
            for topic_idx in (1, 2):
                if len(log["topics"]) <= topic_idx:
                    continue
                candidate = _topic_to_address(log["topics"][topic_idx])
                candidate_lower = candidate.lower()
                if candidate_lower in seen or candidate == Web3.to_checksum_address(_SINK_ADDRESS):
                    continue
                seen.add(candidate_lower)
                checked += 1
                try:
                    if token_contract.functions.balanceOf(candidate).call() >= min_balance_raw:
                        return candidate
                except Exception:
                    continue
            if checked >= max_candidates:
                break
        if checked >= max_candidates:
            break

    raise RuntimeError(
        f"Could not find a recent holder with at least {min_balance_raw} units "
        f"of token {token_contract.address} in the last {lookback_blocks} blocks."
    )


# =============================================================================
# Price seeding
# =============================================================================


async def seed_prices(
    repo: DatabaseRepository,
    quote: PriceQuote,
    *,
    count: int,
    source: str = "uni-base_pool",
    spacing_ms: int = 60_000,
) -> list[PriceQuote]:
    """Seed `count` identical price snapshots into the price store at evenly spaced timestamps."""
    now_ms = int(time.time() * 1000)
    seeded: list[PriceQuote] = []
    for idx in range(count):
        seeded_quote = PriceQuote(
            source=source,
            timestamp=now_ms - ((count - idx) * spacing_ms),
            bid=quote.bid,
            ask=quote.ask,
            mid=quote.mid,
        )
        await repo.prices.insert_price_snapshot(seeded_quote)
        seeded.append(seeded_quote)
    return seeded
