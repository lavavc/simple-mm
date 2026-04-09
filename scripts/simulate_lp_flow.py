#!/usr/bin/env python3
"""Trace the uni-base LP flow on an existing Base Anvil fork."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.accounts import AccountManager, AccountRole
from engine.config import settings
from engine.db.repository import DatabaseRepository, open_repository
from engine.lp import strategy
from engine.lp.rebalancer import LPRebalancer
from engine.lp.uniswap_v4 import V4PositionManager
from engine.types import PriceQuote
from engine.venues.dex.shared import tick_to_price
from engine.venues.dex.uniswap_base import UniswapBaseV4Adapter

_ANVIL_DEFAULT_SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
_SINK_ADDRESS = "0x000000000000000000000000000000000000dEaD"
_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()


def _decimal_to_str(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _decimal_to_str(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decimal_to_str(item) for item in value]
    return value


def _print_section(title: str, payload: Any) -> None:
    print(f"[{title}]")
    print(json.dumps(_decimal_to_str(payload), indent=2, sort_keys=True))


def _build_account_manager() -> AccountManager:
    if not settings.wallet_mnemonic and not settings.use_test_accounts:
        raise RuntimeError(
            "AccountManager requires WALLET_MNEMONIC or USE_TEST_ACCOUNTS=true for this script."
        )
    return AccountManager(
        mnemonic=settings.wallet_mnemonic if settings.wallet_mnemonic else None,
        use_test_accounts=settings.use_test_accounts,
    )


def _build_adapter(rpc_url: str) -> tuple[UniswapBaseV4Adapter, AccountManager]:
    account_manager = _build_account_manager()
    lp_key = account_manager.get_private_key(AccountRole.UNI_BASE_LP)
    trade_key = account_manager.get_private_key(AccountRole.UNI_BASE_TRADE)
    adapter = UniswapBaseV4Adapter(
        lp_private_key=lp_key,
        trade_private_key=trade_key,
        rpc_url=rpc_url,
        params=settings.uni_base_lp_params,
    )
    return adapter, account_manager


def _build_lp_manager(adapter: UniswapBaseV4Adapter) -> V4PositionManager:
    pm_contract = adapter.w3.eth.contract(
        address=Web3.to_checksum_address(adapter.config.position_manager),
        abi=V4PositionManager.POSITION_MANAGER_ABI,
    )
    return V4PositionManager(
        config=adapter.config,
        state_view=adapter.state_view,
        position_manager_contract=pm_contract,
        params=settings.uni_base_lp_params,
        venue_name="uni-base",
        tx_context=adapter,
    )


def _build_rebalancer(repo: DatabaseRepository) -> LPRebalancer:
    return LPRebalancer(
        broadcast=lambda _event: None,
        price_store=repo.prices,
        venue_config_store=repo.venue_config,
        action_store=repo.actions,
        auto_management_enabled=lambda: True,
    )


def _require_connected_anvil(adapter: UniswapBaseV4Adapter) -> dict[str, Any]:
    if not adapter.w3.is_connected():
        raise RuntimeError(f"Could not connect to fork RPC at {adapter.config.rpc_url}")
    latest = adapter.w3.eth.get_block("latest")
    environment = {
        "rpc_url": adapter.config.rpc_url,
        "chain_id": adapter.config.chain_id,
        "actual_chain_id": adapter.w3.eth.chain_id,
        "block_number": latest["number"],
        "block_timestamp": latest["timestamp"],
        "lp_wallet": adapter.lp_account.address,
        "trade_wallet": adapter.trade_account.address,
        "position_manager": adapter.config.position_manager,
        "pool_manager": adapter.config.pool_manager,
        "state_view": adapter.config.state_view,
        "token0_address": adapter.config.token0_address,
        "token1_address": adapter.config.token1_address,
        "pool_id": adapter.config.pool_id,
    }
    required_contracts = {
        "pool_manager": adapter.config.pool_manager,
        "state_view": adapter.config.state_view,
        "position_manager": adapter.config.position_manager,
        "token0": adapter.config.token0_address,
        "token1": adapter.config.token1_address,
    }
    missing = [
        name
        for name, address in required_contracts.items()
        if len(adapter.w3.eth.get_code(Web3.to_checksum_address(address))) == 0
    ]
    if missing:
        raise RuntimeError(
            "RPC is reachable, but it does not appear to be a Base mainnet fork. "
            f"Missing contract code for: {', '.join(missing)}. "
            "Start Anvil against Base mainnet, for example: "
            f"anvil --fork-url {settings.base_rpc_url} --port 8545 --silent"
        )
    return environment


def _read_wallet_state(adapter: UniswapBaseV4Adapter) -> dict[str, Any]:
    native_raw = adapter.w3.eth.get_balance(adapter.lp_account.address)
    token0_raw = adapter.token0.functions.balanceOf(adapter.lp_account.address).call()
    token1_raw = adapter.token1.functions.balanceOf(adapter.lp_account.address).call()
    token0_human = Decimal(token0_raw) / Decimal(10 ** adapter.config.token0_decimals)
    token1_human = Decimal(token1_raw) / Decimal(10 ** adapter.config.token1_decimals)
    native_human = Decimal(native_raw) / Decimal(10 ** 18)
    return {
        "address": adapter.lp_account.address,
        "native_symbol": "ETH",
        "native": {"raw": native_raw, "human": native_human},
        "token0": {
            "symbol": adapter.config.token0_symbol,
            "raw": token0_raw,
            "human": token0_human,
        },
        "token1": {
            "symbol": adapter.config.token1_symbol,
            "raw": token1_raw,
            "human": token1_human,
        },
    }


def _value_wallet_state(adapter: UniswapBaseV4Adapter, wallet_state: dict[str, Any], cngn_price_usd: Decimal) -> Decimal:
    token0_value = Decimal(0)
    token1_value = Decimal(0)
    if adapter.config.cngn_is_token0:
        token0_value = wallet_state["token0"]["human"] * cngn_price_usd
        token1_value = wallet_state["token1"]["human"]
    else:
        token0_value = wallet_state["token0"]["human"]
        token1_value = wallet_state["token1"]["human"] * cngn_price_usd
    return token0_value + token1_value


def _wallet_snapshot_from_raw(adapter: UniswapBaseV4Adapter, amount0_raw: int, amount1_raw: int) -> dict[str, Any]:
    return {
        "token0": {
            "symbol": adapter.config.token0_symbol,
            "raw": amount0_raw,
            "human": Decimal(amount0_raw) / Decimal(10 ** adapter.config.token0_decimals),
        },
        "token1": {
            "symbol": adapter.config.token1_symbol,
            "raw": amount1_raw,
            "human": Decimal(amount1_raw) / Decimal(10 ** adapter.config.token1_decimals),
        },
    }


def _display_range(config: Any, tick_lower: int, tick_upper: int) -> tuple[Decimal, Decimal]:
    if config.invert_price:
        return (
            Decimal(1) / tick_to_price(tick_upper, config.token0_decimals, config.token1_decimals),
            Decimal(1) / tick_to_price(tick_lower, config.token0_decimals, config.token1_decimals),
        )
    return (
        tick_to_price(tick_lower, config.token0_decimals, config.token1_decimals),
        tick_to_price(tick_upper, config.token0_decimals, config.token1_decimals),
    )


def _fund_native_balance(w3: Web3, address: str, amount_eth: Decimal) -> None:
    amount_wei = int(amount_eth * Decimal(10 ** 18))
    response = w3.provider.make_request("anvil_setBalance", [address, hex(amount_wei)])
    if response.get("error"):
        raise RuntimeError(f"anvil_setBalance failed: {response['error']}")


def _topic_to_address(topic: Any) -> str:
    topic_hex = Web3.to_hex(topic)
    return Web3.to_checksum_address("0x" + topic_hex[-40:])


def _build_unlocked_tx_params(w3: Web3, from_address: str) -> dict[str, Any]:
    checksum = Web3.to_checksum_address(from_address)
    block = w3.eth.get_block("latest")
    if "baseFeePerGas" in block:
        base_fee = int(block["baseFeePerGas"])
    else:
        base_fee = int(w3.eth.gas_price)
    priority_fee = int(w3.to_wei(0.1, "gwei"))
    return {
        "from": checksum,
        "nonce": w3.eth.get_transaction_count(checksum, "pending"),
        "chainId": w3.eth.chain_id,
        "maxFeePerGas": (2 * base_fee) + priority_fee,
        "maxPriorityFeePerGas": priority_fee,
        "value": 0,
    }


@contextlib.contextmanager
def _impersonated_account(w3: Web3, address: str):
    checksum = Web3.to_checksum_address(address)
    response = w3.provider.make_request("anvil_impersonateAccount", [checksum])
    if response.get("error"):
        raise RuntimeError(f"anvil_impersonateAccount failed for {checksum}: {response['error']}")
    try:
        yield checksum
    finally:
        w3.provider.make_request("anvil_stopImpersonatingAccount", [checksum])


async def _transfer_erc20_from_lp(
    adapter: UniswapBaseV4Adapter,
    token_contract: Any,
    amount_raw: int,
    *,
    recipient: str,
) -> str:
    if amount_raw <= 0:
        return ""

    tx_params = adapter._get_tx_params(adapter.lp_account)
    tx_params["value"] = 0
    tx_params["gas"] = 200_000
    tx = token_contract.functions.transfer(
        Web3.to_checksum_address(recipient),
        int(amount_raw),
    ).build_transaction(tx_params)
    estimated = adapter.w3.eth.estimate_gas(
        {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": tx.get("value", 0),
        }
    )
    tx["gas"] = int(estimated * 1.2)
    result = await adapter._send_transaction(tx, adapter.lp_account)
    if result.status != "confirmed":
        raise RuntimeError(
            f"Failed to drain {token_contract.address} from LP wallet: {result.error or 'unknown error'}"
        )
    return result.hash


def _transfer_erc20_from_unlocked(
    w3: Web3,
    token_contract: Any,
    *,
    sender: str,
    recipient: str,
    amount_raw: int,
) -> str:
    if amount_raw <= 0:
        return ""

    checksum_sender = Web3.to_checksum_address(sender)
    _fund_native_balance(w3, checksum_sender, Decimal("1"))
    tx_params = _build_unlocked_tx_params(w3, checksum_sender)
    tx = token_contract.functions.transfer(
        Web3.to_checksum_address(recipient),
        int(amount_raw),
    ).build_transaction(tx_params)
    estimated = w3.eth.estimate_gas(
        {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": tx.get("value", 0),
        }
    )
    tx["gas"] = int(estimated * 1.2)
    tx_hash = w3.eth.send_transaction(tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(
            f"ERC20 transfer from impersonated donor {checksum_sender} failed"
        )
    return Web3.to_hex(tx_hash)


def _find_token_donor(
    w3: Web3,
    token_contract: Any,
    *,
    min_balance_raw: int,
    exclude: set[str],
    lookback_blocks: int = 2_000,
    batch_size: int = 50,
    max_candidates: int = 300,
) -> str:
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
        if checked >= max_candidates:
            break

    raise RuntimeError(
        f"Could not find a recent holder with at least {min_balance_raw} units of token "
        f"{token_contract.address} in the last {lookback_blocks} blocks."
    )


async def _fund_lp_wallet_for_swap(
    adapter: UniswapBaseV4Adapter,
    *,
    usdc_amount: Decimal,
    eth_amount: Decimal,
) -> dict[str, Any]:
    _fund_native_balance(adapter.w3, adapter.lp_account.address, Decimal("1"))

    wallet_before = _read_wallet_state(adapter)
    drain_txs: list[dict[str, Any]] = []
    for token_contract, token_key in (
        (adapter.token0, "token0"),
        (adapter.token1, "token1"),
    ):
        amount_raw = int(wallet_before[token_key]["raw"])
        if amount_raw <= 0:
            continue
        tx_hash = await _transfer_erc20_from_lp(
            adapter,
            token_contract,
            amount_raw,
            recipient=_SINK_ADDRESS,
        )
        drain_txs.append(
            {
                "token": wallet_before[token_key]["symbol"],
                "amount_raw": amount_raw,
                "tx_hash": tx_hash,
            }
        )

    drained_state = _read_wallet_state(adapter)
    if drained_state["token0"]["raw"] != 0 or drained_state["token1"]["raw"] != 0:
        raise RuntimeError(
            "Failed to drain the LP wallet to a clean token state before funding."
        )

    target_usdc_raw = int(usdc_amount * Decimal(10 ** adapter.config.token1_decimals))
    donor = _find_token_donor(
        adapter.w3,
        adapter.token1,
        min_balance_raw=target_usdc_raw,
        exclude={
            adapter.lp_account.address,
            adapter.trade_account.address,
            _ANVIL_DEFAULT_SENDER,
            _SINK_ADDRESS,
        },
    )
    with _impersonated_account(adapter.w3, donor):
        fund_tx_hash = _transfer_erc20_from_unlocked(
            adapter.w3,
            adapter.token1,
            sender=donor,
            recipient=adapter.lp_account.address,
            amount_raw=target_usdc_raw,
        )

    _fund_native_balance(adapter.w3, adapter.lp_account.address, eth_amount)
    funded_state = _read_wallet_state(adapter)
    if funded_state["token0"]["raw"] != 0:
        raise RuntimeError(
            f"Expected zero {adapter.config.token0_symbol} after funding, found {funded_state['token0']['raw']}."
        )
    if funded_state["token1"]["raw"] != target_usdc_raw:
        raise RuntimeError(
            f"Expected exact {target_usdc_raw} raw {adapter.config.token1_symbol} after funding, "
            f"found {funded_state['token1']['raw']}."
        )

    return {
        "target": {
            "eth": eth_amount,
            adapter.config.token0_symbol: Decimal("0"),
            adapter.config.token1_symbol: usdc_amount,
        },
        "drain_transactions": drain_txs,
        "donor": donor,
        "fund_tx_hash": fund_tx_hash,
    }


async def _seed_prices(
    repo: DatabaseRepository,
    quote: PriceQuote,
    *,
    count: int,
    spacing_ms: int = 60_000,
) -> list[PriceQuote]:
    now_ms = int(time.time() * 1000)
    seeded: list[PriceQuote] = []
    for idx in range(count):
        seeded_quote = PriceQuote(
            source="uni-base_pool",
            timestamp=now_ms - ((count - idx) * spacing_ms),
            bid=quote.bid,
            ask=quote.ask,
            mid=quote.mid,
        )
        await repo.prices.insert_price_snapshot(seeded_quote)
        seeded.append(seeded_quote)
    return seeded


async def _collect_actions(repo: DatabaseRepository) -> list[dict[str, Any]]:
    actions = await repo.actions.get_actions(venue="uni-base", limit=50)
    return sorted(actions, key=lambda item: item["timestamp"])


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.do_withdraw and not args.withdraw_to:
        raise RuntimeError("--do-withdraw requires --withdraw-to")

    adapter, _account_manager = _build_adapter(args.rpc_url)
    lp_manager = _build_lp_manager(adapter)

    report: dict[str, Any] = {"environment": _require_connected_anvil(adapter)}

    with tempfile.TemporaryDirectory(prefix="lp-flow-trace-") as temp_dir:
        db_path = str(Path(temp_dir) / "trace.db")
        repo = await open_repository(db_path)
        report["environment"]["db_path"] = db_path
        rebalancer = _build_rebalancer(repo)
        try:
            owned_before = lp_manager.get_owned_positions()
            report["wallet_before"] = _read_wallet_state(adapter)
            report["wallet_before"]["owned_positions"] = owned_before
            if owned_before:
                raise RuntimeError(
                    f"Fork wallet already owns LP NFTs: {owned_before}. Start from a clean fork."
                )

            report["funding"] = await _fund_lp_wallet_for_swap(
                adapter,
                usdc_amount=args.usdc,
                eth_amount=args.eth,
            )
            report["wallet_after_fund"] = _read_wallet_state(adapter)

            quote = await adapter.get_current_price()
            if quote is None:
                raise RuntimeError("Could not fetch live uni-base spot price from fork")
            seeded_quotes = await _seed_prices(repo, quote, count=args.seed_prices)
            seeded_prices = await repo.prices.get_recent_prices_for_source("uni-base_pool", limit=args.seed_prices)
            if len(seeded_prices) < 10:
                raise RuntimeError(
                    f"Seeded only {len(seeded_prices)} usable uni-base_pool prices; need at least 10."
                )
            tick_lower, tick_upper = strategy.calculate_tick_range(
                seeded_prices,
                lp_manager.params,
                adapter.config.tick_spacing or 0,
                adapter.config.token0_decimals,
                adapter.config.token1_decimals,
                invert_price=adapter.config.invert_price,
                venue_name=lp_manager.name,
            )
            range_min, range_max = _display_range(adapter.config, tick_lower, tick_upper)
            report["price_seed"] = {
                "count": len(seeded_quotes),
                "source": "uni-base_pool",
                "spot_mid": quote.mid,
                "first_timestamp": seeded_quotes[0].timestamp,
                "last_timestamp": seeded_quotes[-1].timestamp,
            }
            report["prices_seeded"] = report["price_seed"]
            report["pre_mint_snapshot"] = {
                "spot_mid": quote.mid,
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "range_min": range_min,
                "range_max": range_max,
                "wallet": report["wallet_after_fund"],
            }

            created = await rebalancer.create_position(lp_manager, triggered_by="script:simulate_lp_flow")
            actions = await _collect_actions(repo)
            report["actions"] = actions

            ratio_actions = [action for action in actions if action["action_type"] == "lp_ratio_swap"]
            if not ratio_actions:
                raise RuntimeError(
                    "No lp_ratio_swap action was recorded. The script expected a real swap from the exact 200 USDC start state."
                )
            mint_actions = [action for action in actions if action["action_type"] == "mint_position"]
            if not mint_actions:
                raise RuntimeError("No mint_position action was recorded.")
            mint_action = mint_actions[-1]
            if mint_action["status"] != "confirmed":
                raise RuntimeError(
                    f"Mint failed: {mint_action.get('error') or 'unknown error'}"
                )

            token_ids = lp_manager.get_owned_positions()
            if len(token_ids) != 1:
                raise RuntimeError(
                    f"Expected exactly one LP NFT after mint, found {len(token_ids)}: {token_ids}"
                )

            report["wallet_after_mint"] = _read_wallet_state(adapter)
            report["wallet_after_mint"]["owned_positions"] = token_ids
            report["wallet_after"] = report["wallet_after_mint"]

            mint_amount0_raw = int(mint_action["metadata"]["amount0_raw"])
            mint_amount1_raw = int(mint_action["metadata"]["amount1_raw"])
            report["wallet_after_ratio_swap"] = _wallet_snapshot_from_raw(
                adapter,
                mint_amount0_raw + int(report["wallet_after_mint"]["token0"]["raw"]),
                mint_amount1_raw + int(report["wallet_after_mint"]["token1"]["raw"]),
            )

            position = await lp_manager.get_position_as_schema()
            report["position_after_mint"] = position.model_dump()
            report["position"] = report["position_after_mint"]

            if not created:
                raise RuntimeError("LPRebalancer.create_position() returned False despite a confirmed mint.")
            if position.lp_position is None or position.lp_position.token_id is None:
                raise RuntimeError("Mint confirmed, but LP position schema does not expose a token_id.")

            post_mint_quote = await adapter.get_current_price()
            if post_mint_quote is None:
                raise RuntimeError("Could not fetch uni-base spot price after mint.")
            deployed_value_usd = position.position_value_usd or Decimal(0)
            idle_value_usd = _value_wallet_state(adapter, report["wallet_after_mint"], post_mint_quote.mid)
            total_value_usd = deployed_value_usd + idle_value_usd
            report["valuation"] = {
                "spot_mid": post_mint_quote.mid,
                "deployed_value_usd": deployed_value_usd,
                "idle_value_usd": idle_value_usd,
                "total_value_usd": total_value_usd,
                "underdeployed_value_usd": idle_value_usd,
            }
            report["deployed_value_usd"] = deployed_value_usd
            report["idle_value_usd"] = idle_value_usd
            report["total_value_usd"] = total_value_usd

            if args.do_withdraw:
                withdraw_results = await rebalancer.withdraw_positions(
                    lp_manager,
                    recipient=args.withdraw_to,
                    action_type="manual_withdraw",
                    triggered_by="script:withdraw",
                )
                report["withdraw"] = {
                    "recipient": args.withdraw_to,
                    "results": withdraw_results,
                    "owned_positions_after": lp_manager.get_owned_positions(),
                    "wallet_after_withdraw": _read_wallet_state(adapter),
                }
                if report["withdraw"]["owned_positions_after"]:
                    raise RuntimeError(
                        f"Withdraw completed but positions remain: {report['withdraw']['owned_positions_after']}"
                    )

            return report
        finally:
            await repo.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trace the uni-base LP flow against an already-running Base Anvil fork."
    )
    parser.add_argument("--rpc-url", required=True, help="An existing Base Anvil fork RPC URL")
    parser.add_argument("--usdc", type=Decimal, default=Decimal("200"))
    parser.add_argument("--eth", type=Decimal, default=Decimal("0.1"))
    parser.add_argument("--seed-prices", type=int, default=20)
    parser.add_argument("--withdraw-to")
    parser.add_argument("--do-withdraw", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        report = asyncio.run(_run(args))
    except Exception as exc:
        error_report = {"error": str(exc)}
        if args.json:
            print(json.dumps(_decimal_to_str(error_report), indent=2, sort_keys=True))
        else:
            _print_section("error", error_report)
        raise SystemExit(1) from exc

    if args.json:
        print(json.dumps(_decimal_to_str(report), indent=2, sort_keys=True))
        return

    for section in [
        "environment",
        "wallet_before",
        "price_seed",
        "wallet_after_fund",
        "pre_mint_snapshot",
        "actions",
        "wallet_after_ratio_swap",
        "wallet_after_mint",
        "position_after_mint",
        "valuation",
        "withdraw",
    ]:
        if section in report:
            _print_section(section, report[section])


if __name__ == "__main__":
    main()
