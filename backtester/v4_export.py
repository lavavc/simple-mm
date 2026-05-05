"""Historical Uniswap v4 event export for backtester datasets."""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from eth_abi import decode  # type: ignore[attr-defined]
from requests import HTTPError
from web3 import Web3
try:
    from web3.middleware import ExtraDataToPOAMiddleware as POA_MIDDLEWARE
except ImportError:  # web3.py v6
    from web3.middleware import geth_poa_middleware as POA_MIDDLEWARE
from engine.config import settings

_V4_LP_INCREASE_LIQUIDITY = 0
_V4_LP_DECREASE_LIQUIDITY = 1
_V4_LP_MINT_POSITION = 2
_V4_LP_BURN_POSITION = 3
V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
_Q96 = 2**96

POSITION_MANAGER_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "getPositionLiquidity",
        "outputs": [{"name": "liquidity", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "name": "tokenOfOwnerByIndex",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "getPoolAndPositionInfo",
        "outputs": [
            {
                "components": [
                    {"name": "currency0", "type": "address"},
                    {"name": "currency1", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "tickSpacing", "type": "int24"},
                    {"name": "hooks", "type": "address"},
                ],
                "name": "poolKey",
                "type": "tuple",
            },
            {"name": "info", "type": "bytes32"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "unlockData", "type": "bytes"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "modifyLiquidities",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]
STATE_VIEW_ABI = [
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getSlot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "protocolFee", "type": "uint24"},
            {"name": "lpFee", "type": "uint24"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getLiquidity",
        "outputs": [{"name": "liquidity", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
]
from engine.web3_utils import as_hexstr, coerce_hex_str

_TRANSFER_EVENT_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex().lower()


def _tick_to_sqrt_price_x96(tick: int) -> int:
    return int((1.0001 ** (tick / 2)) * _Q96)


def sqrt_price_x96_to_decimal(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> Decimal:
    price = (Decimal(sqrt_price_x96) / Decimal(_Q96)) ** 2
    decimal_diff = token0_decimals - token1_decimals
    price *= Decimal(10**decimal_diff)
    return price


@dataclass(frozen=True)
class ExportPoolConfig:
    name: str
    chain: str
    rpc_url: str
    pool_manager: str
    state_view: str
    pool_id: str
    position_manager: str
    token0_address: str
    token1_address: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    fee_rate: float
    invert_price: bool
    default_start_block: int
    chunk_size: int = 10_000


@dataclass
class PositionTokenState:
    pool_id: str
    tick_lower: int
    tick_upper: int
    liquidity: int


@dataclass(frozen=True)
class ExportRow:
    block_time: str
    chain: str
    pool_id: str
    event_type: str
    tx_hash: str
    log_index: int
    block_number: int
    sqrt_price_x96: int
    tick: int
    active_liquidity: int
    fee_rate: float
    amount0: float
    amount1: float
    amount_usd: float
    cngn_usd_price: float
    token0_symbol: str
    token1_symbol: str


POOL_CONFIGS = {
    "uni-base": ExportPoolConfig(
        name="uni-base",
        chain="base",
        rpc_url=settings.base_rpc_url,
        pool_manager=settings.uni_base_pool_manager,
        state_view=settings.uni_base_state_view,
        pool_id=settings.uni_base_pool_id,
        position_manager="0x7c5f5a4bbd8fd63184577525326123b519429bdc",
        token0_address=settings.cngn_base_address,
        token1_address=settings.usdc_base_address,
        token0_symbol="cNGN",
        token1_symbol="USDC",
        token0_decimals=6,
        token1_decimals=6,
        fee_rate=1500 / 1_000_000,
        invert_price=False,
        default_start_block=40_255_567,
        chunk_size=5_000,
    ),
    "uni-bsc": ExportPoolConfig(
        name="uni-bsc",
        chain="bsc",
        rpc_url=settings.bsc_rpc_url,
        pool_manager=settings.uni_bsc_pool_manager,
        state_view=settings.uni_bsc_state_view,
        pool_id=settings.uni_bsc_pool_id,
        position_manager="0x7a4a5c919ae2541aed11041a1aeee68f1287f95b",
        token0_address=settings.usdt_bsc_address,
        token1_address=settings.cngn_bsc_address,
        token0_symbol="USDT",
        token1_symbol="cNGN",
        token0_decimals=18,
        token1_decimals=6,
        fee_rate=1200 / 1_000_000,
        invert_price=True,
        default_start_block=73_736_863,
        chunk_size=5_000,
    ),
}


def _make_web3(config: ExportPoolConfig) -> Web3:
    w3 = Web3(Web3.HTTPProvider(config.rpc_url))
    if config.chain == "bsc":
        w3.middleware_onion.inject(POA_MIDDLEWARE, layer=0)
    return w3


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _fetch_logs_with_debug(w3: Web3, params: dict[str, Any], context: str) -> list[Any]:
    try:
        return w3.eth.get_logs(params)
    except HTTPError as exc:
        response = getattr(exc, "response", None)
        body = ""
        if response is not None:
            try:
                body = response.text
            except Exception:
                body = "<unavailable>"
        _log(f"{context}: HTTPError {exc}. params={params}. body={body}")
        raise


def _rpc_block_tag(block_number: int | str) -> str:
    if isinstance(block_number, str):
        return block_number
    return hex(block_number)


def _raw_get_block(w3: Web3, block_number: int | str, full_transactions: bool) -> dict[str, Any]:
    response = w3.provider.make_request(
        "eth_getBlockByNumber",
        [_rpc_block_tag(block_number), full_transactions],
    )
    result = response.get("result")
    if result is None:
        raise ValueError(f"eth_getBlockByNumber returned no result for {block_number}: {response}")
    return result


def _block_number_from_raw(block: dict[str, Any]) -> int:
    return int(block["number"], 16)


def _block_timestamp_from_raw(block: dict[str, Any]) -> int:
    return int(block["timestamp"], 16)


def _int_from_rpc(value: Any) -> int:
    if isinstance(value, str):
        if value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value)
    return int(value)


def _int128_from_word(word: bytes) -> int:
    return int.from_bytes(word, "big", signed=True)


def _datetime_from_block_ts(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _stable_and_cngn_amounts(amount0: Decimal, amount1: Decimal, config: ExportPoolConfig) -> tuple[Decimal, Decimal]:
    if config.token0_symbol == "cNGN":
        return abs(amount1), abs(amount0)
    return abs(amount0), abs(amount1)


def derive_cngn_price(amount0_raw: int, amount1_raw: int, config: ExportPoolConfig) -> Decimal:
    amount0 = Decimal(abs(amount0_raw)) / Decimal(10 ** config.token0_decimals)
    amount1 = Decimal(abs(amount1_raw)) / Decimal(10 ** config.token1_decimals)
    stable_amount, cngn_amount = _stable_and_cngn_amounts(amount0, amount1, config)
    if cngn_amount <= 0:
        return Decimal("0")
    return stable_amount / cngn_amount


def decode_swap_row(log: dict[str, Any], block_timestamp: int, config: ExportPoolConfig) -> ExportRow:
    data_hex = coerce_hex_str(log["data"])
    data_bytes = bytes.fromhex(data_hex[2:])
    amount0_raw = _int128_from_word(data_bytes[0:32])
    amount1_raw = _int128_from_word(data_bytes[32:64])
    sqrt_price_x96 = int.from_bytes(data_bytes[64:96], "big")
    active_liquidity = int.from_bytes(data_bytes[96:128], "big")
    tick = int.from_bytes(data_bytes[128:160], "big", signed=True)
    fee_word = int.from_bytes(data_bytes[160:192], "big")
    cngn_price = derive_cngn_price(amount0_raw, amount1_raw, config)
    amount0 = Decimal(amount0_raw) / Decimal(10 ** config.token0_decimals)
    amount1 = Decimal(amount1_raw) / Decimal(10 ** config.token1_decimals)
    stable_amount, _ = _stable_and_cngn_amounts(amount0, amount1, config)
    return ExportRow(
        block_time=_datetime_from_block_ts(block_timestamp),
        chain=config.chain,
        pool_id=config.pool_id,
        event_type="swap",
        tx_hash=coerce_hex_str(log["transactionHash"]),
        log_index=int(log["logIndex"]),
        block_number=int(log["blockNumber"]),
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        active_liquidity=active_liquidity,
        fee_rate=(fee_word / 1_000_000) if fee_word else config.fee_rate,
        amount0=float(amount0),
        amount1=float(amount1),
        amount_usd=float(stable_amount),
        cngn_usd_price=float(cngn_price),
        token0_symbol=config.token0_symbol,
        token1_symbol=config.token1_symbol,
    )


def _pool_key_matches(pool_key: tuple[Any, ...], config: ExportPoolConfig) -> bool:
    return (
        Web3.to_checksum_address(pool_key[0]) == Web3.to_checksum_address(config.token0_address)
        and Web3.to_checksum_address(pool_key[1]) == Web3.to_checksum_address(config.token1_address)
        and int(pool_key[2]) == int(config.fee_rate * 1_000_000)
    )


def decode_modify_liquidities_payload(input_data: str) -> tuple[bytes, list[bytes]]:
    raw = bytes.fromhex(coerce_hex_str(input_data)[10:])
    unlock_data, _deadline = decode(["bytes", "uint256"], raw)
    actions, params = decode(["bytes", "bytes[]"], unlock_data)
    return actions, list(params)


def _decode_mint_param(raw: bytes) -> tuple[tuple[Any, ...], int, int, int, int, int]:
    pool_key, tick_lower, tick_upper, liquidity, amount0_max, amount1_max, _recipient, _hook = decode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        raw,
    )
    return pool_key, int(tick_lower), int(tick_upper), int(liquidity), int(amount0_max), int(amount1_max)


def _decode_increase_or_decrease_param(raw: bytes) -> tuple[int, int]:
    token_id, liquidity_delta, _amount0, _amount1, _hook = decode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        raw,
    )
    return int(token_id), int(liquidity_delta)


def _decode_burn_param(raw: bytes) -> int:
    token_id, _amount0, _amount1, _hook = decode(["uint256", "uint128", "uint128", "bytes"], raw)
    return int(token_id)


def _amounts_from_liquidity(
    liquidity: int,
    tick_lower: int,
    tick_upper: int,
    sqrt_price_x96: int,
    config: ExportPoolConfig,
) -> tuple[Decimal, Decimal]:
    sqrt_lower = _tick_to_sqrt_price_x96(tick_lower)
    sqrt_upper = _tick_to_sqrt_price_x96(tick_upper)
    t0_scale = Decimal(10 ** config.token0_decimals)
    t1_scale = Decimal(10 ** config.token1_decimals)
    if sqrt_price_x96 <= sqrt_lower:
        amount0 = Decimal(liquidity * _Q96 * (sqrt_upper - sqrt_lower) // (sqrt_lower * sqrt_upper)) / t0_scale
        amount1 = Decimal(0)
    elif sqrt_price_x96 >= sqrt_upper:
        amount0 = Decimal(0)
        amount1 = Decimal(liquidity * (sqrt_upper - sqrt_lower) // _Q96) / t1_scale
    else:
        amount0 = Decimal(liquidity * _Q96 * (sqrt_upper - sqrt_price_x96) // (sqrt_price_x96 * sqrt_upper)) / t0_scale
        amount1 = Decimal(liquidity * (sqrt_price_x96 - sqrt_lower) // _Q96) / t1_scale
    return amount0, amount1


def _state_at_block(state_view: Any, config: ExportPoolConfig, block_number: int) -> tuple[int, int, float]:
    pool_id_bytes = bytes.fromhex(config.pool_id[2:])
    slot0 = state_view.functions.getSlot0(pool_id_bytes).call(block_identifier=block_number)
    sqrt_price_x96 = int(slot0[0])
    tick = int(slot0[1])
    liquidity = int(state_view.functions.getLiquidity(pool_id_bytes).call(block_identifier=block_number))
    cngn_price = sqrt_price_x96_to_decimal(sqrt_price_x96, config.token0_decimals, config.token1_decimals)
    if config.invert_price:
        cngn_price = Decimal(1) / cngn_price if cngn_price > 0 else Decimal(0)
    return sqrt_price_x96, tick, liquidity, float(cngn_price)


def _find_minted_token_id(receipt: dict[str, Any], position_manager: str) -> int | None:
    for log in receipt["logs"]:
        if Web3.to_checksum_address(log["address"]) != Web3.to_checksum_address(position_manager):
            continue
        topics = [coerce_hex_str(topic).lower() for topic in log["topics"]]
        if not topics or topics[0] != _TRANSFER_EVENT_TOPIC:
            continue
        return int(coerce_hex_str(topics[3]), 16)
    return None


def find_initialize_block(w3: Web3, config: ExportPoolConfig) -> int:
    event_topic = as_hexstr(
        w3.keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
    )
    latest_block = int(w3.eth.block_number)
    step = max(config.chunk_size, 10_000)
    for chunk_start in range(0, latest_block + 1, step):
        chunk_end = min(chunk_start + step - 1, latest_block)
        logs = _fetch_logs_with_debug(w3,
            {
                "address": Web3.to_checksum_address(config.pool_manager),
                "topics": [event_topic, as_hexstr(config.pool_id)],
                "fromBlock": chunk_start,
                "toBlock": chunk_end,
            },
            context=f"[{config.name}] initialize lookup {chunk_start:,}->{chunk_end:,}",
        )
        if logs:
            return int(logs[0]["blockNumber"])
    raise ValueError(f"Initialize event not found for {config.name} ({config.pool_id})")


def build_liquidity_rows_for_tx(
    tx: dict[str, Any],
    receipt: dict[str, Any],
    block_timestamp: int,
    state_view: Any,
    config: ExportPoolConfig,
    token_state: dict[int, PositionTokenState],
) -> list[ExportRow]:
    rows: list[ExportRow] = []
    actions, params = decode_modify_liquidities_payload(tx["input"])
    if not actions:
        return rows
    sqrt_price_x96, tick, active_liquidity, cngn_usd_price = _state_at_block(
        state_view, config, _int_from_rpc(tx["blockNumber"])
    )

    for action, raw in zip(actions, params):
        action_code = action if isinstance(action, int) else action
        if isinstance(action_code, bytes):
            action_code = action_code[0]
        if action_code == _V4_LP_MINT_POSITION:
            pool_key, tick_lower, tick_upper, liquidity_delta, amount0_max, amount1_max = _decode_mint_param(raw)
            if not _pool_key_matches(pool_key, config):
                continue
            token_id = _find_minted_token_id(receipt, config.position_manager)
            if token_id is not None:
                token_state[token_id] = PositionTokenState(
                    pool_id=config.pool_id,
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    liquidity=liquidity_delta,
                )
            amount0 = Decimal(amount0_max) / Decimal(10 ** config.token0_decimals)
            amount1 = Decimal(amount1_max) / Decimal(10 ** config.token1_decimals)
            stable_amount, _ = _stable_and_cngn_amounts(amount0, amount1, config)
            rows.append(
                ExportRow(
                    block_time=_datetime_from_block_ts(block_timestamp),
                    chain=config.chain,
                    pool_id=config.pool_id,
                    event_type="mint",
                    tx_hash=coerce_hex_str(tx["hash"]),
                    log_index=int(receipt["logs"][-1]["logIndex"]) if receipt["logs"] else 0,
                    block_number=_int_from_rpc(tx["blockNumber"]),
                    sqrt_price_x96=sqrt_price_x96,
                    tick=tick,
                    active_liquidity=active_liquidity,
                    fee_rate=config.fee_rate,
                    amount0=float(amount0),
                    amount1=float(amount1),
                    amount_usd=float(stable_amount),
                    cngn_usd_price=cngn_usd_price,
                    token0_symbol=config.token0_symbol,
                    token1_symbol=config.token1_symbol,
                )
            )
        elif action_code in {_V4_LP_INCREASE_LIQUIDITY, _V4_LP_DECREASE_LIQUIDITY}:
            token_id, liquidity_delta = _decode_increase_or_decrease_param(raw)
            position = token_state.get(token_id)
            if position is None or position.pool_id != config.pool_id:
                continue
            amount0, amount1 = _amounts_from_liquidity(
                liquidity_delta,
                position.tick_lower,
                position.tick_upper,
                sqrt_price_x96,
                config,
            )
            stable_amount, _ = _stable_and_cngn_amounts(amount0, amount1, config)
            event_type = "mint" if action_code == _V4_LP_INCREASE_LIQUIDITY else "burn"
            rows.append(
                ExportRow(
                    block_time=_datetime_from_block_ts(block_timestamp),
                    chain=config.chain,
                    pool_id=config.pool_id,
                    event_type=event_type,
                    tx_hash=coerce_hex_str(tx["hash"]),
                    log_index=int(receipt["logs"][-1]["logIndex"]) if receipt["logs"] else 0,
                    block_number=_int_from_rpc(tx["blockNumber"]),
                    sqrt_price_x96=sqrt_price_x96,
                    tick=tick,
                    active_liquidity=active_liquidity,
                    fee_rate=config.fee_rate,
                    amount0=float(amount0),
                    amount1=float(amount1),
                    amount_usd=float(stable_amount),
                    cngn_usd_price=cngn_usd_price,
                    token0_symbol=config.token0_symbol,
                    token1_symbol=config.token1_symbol,
                )
            )
            new_liquidity = position.liquidity + liquidity_delta if event_type == "mint" else max(position.liquidity - liquidity_delta, 0)
            token_state[token_id] = PositionTokenState(
                pool_id=position.pool_id,
                tick_lower=position.tick_lower,
                tick_upper=position.tick_upper,
                liquidity=new_liquidity,
            )
        elif action_code == _V4_LP_BURN_POSITION:
            token_id = _decode_burn_param(raw)
            token_state.pop(token_id, None)
    return rows


def export_pool_history(
    config: ExportPoolConfig,
    output_path: str,
    start_block: int | None,
    end_block: int | None = None,
    rpc_url: str | None = None,
) -> int:
    if rpc_url is not None:
        config = ExportPoolConfig(**{**config.__dict__, "rpc_url": rpc_url})
    started_at = time.time()
    w3 = _make_web3(config)
    state_view = w3.eth.contract(address=Web3.to_checksum_address(config.state_view), abi=STATE_VIEW_ABI)
    position_manager = w3.eth.contract(address=Web3.to_checksum_address(config.position_manager), abi=POSITION_MANAGER_ABI)
    latest_block = int(w3.eth.block_number)
    if start_block is None:
        start_block = config.default_start_block
    end_block = latest_block if end_block is None else end_block
    _log(
        f"[{config.name}] export start: blocks {start_block:,} -> {end_block:,}, "
        f"initial chunk={config.chunk_size:,}, rpc={config.rpc_url}"
    )
    block_timestamps: dict[int, int] = {}
    token_state: dict[int, PositionTokenState] = {}
    rows: list[ExportRow] = []

    chunk_start = start_block
    current_chunk_size = config.chunk_size
    chunk_index = 0
    while chunk_start <= end_block:
        chunk_end = min(chunk_start + current_chunk_size - 1, end_block)
        chunk_index += 1
        _log(
            f"[{config.name}] chunk {chunk_index}: scanning swap logs for "
            f"{chunk_start:,} -> {chunk_end:,} (size={current_chunk_size:,})"
        )
        try:
            swap_logs = _fetch_logs_with_debug(w3,
                {
                    "address": Web3.to_checksum_address(config.pool_manager),
                    "topics": [as_hexstr(V4_SWAP_TOPIC), as_hexstr(config.pool_id)],
                    "fromBlock": chunk_start,
                    "toBlock": chunk_end,
                },
                context=f"[{config.name}] swap logs {chunk_start:,}->{chunk_end:,}",
            )
        except Exception as exc:
            if "413" in str(exc) and current_chunk_size > 100:
                _log(
                    f"[{config.name}] chunk {chunk_index}: RPC 413 for {chunk_start:,}->{chunk_end:,}; "
                    f"reducing chunk size from {current_chunk_size:,} to {max(current_chunk_size // 2, 100):,}"
                )
                current_chunk_size = max(current_chunk_size // 2, 100)
                continue
            raise
        _log(f"[{config.name}] chunk {chunk_index}: fetched {len(swap_logs):,} swap logs")
        for log in swap_logs:
            block_number = int(log["blockNumber"])
            if block_number not in block_timestamps:
                raw_block = _raw_get_block(w3, block_number, False)
                block_timestamps[block_number] = _block_timestamp_from_raw(raw_block)
            rows.append(decode_swap_row(log, block_timestamps[block_number], config))

        _log(
            f"[{config.name}] chunk {chunk_index}: scanning full blocks for modifyLiquidities "
            f"{chunk_start:,} -> {chunk_end:,}"
        )
        tx_matches = 0
        for block_number in range(chunk_start, chunk_end + 1):
            if (block_number - chunk_start) % 500 == 0:
                _log(
                    f"[{config.name}] chunk {chunk_index}: block {block_number:,}/{chunk_end:,}, "
                    f"rows={len(rows):,}, tracked_positions={len(token_state):,}"
                )
            block = _raw_get_block(w3, block_number, True)
            block_timestamps[block_number] = _block_timestamp_from_raw(block)
            for tx in block["transactions"]:
                if not tx.get("to"):
                    continue
                if Web3.to_checksum_address(tx["to"]) != position_manager.address:
                    continue
                if not coerce_hex_str(tx["input"]).startswith(coerce_hex_str(position_manager.functions.modifyLiquidities(b"", 0).selector)):
                    continue
                tx_matches += 1
                receipt = w3.eth.get_transaction_receipt(tx["hash"])
                rows.extend(
                    build_liquidity_rows_for_tx(
                        tx,
                        receipt,
                        block_timestamps[block_number],
                        state_view,
                        config,
                        token_state,
                    )
                )
        elapsed = time.time() - started_at
        _log(
            f"[{config.name}] chunk {chunk_index}: done. modifyLiquidities txs={tx_matches:,}, "
            f"total_rows={len(rows):,}, elapsed={elapsed:.1f}s"
        )
        chunk_start = chunk_end + 1

    rows.sort(key=lambda row: (row.block_time, row.block_number, row.log_index))
    _log(f"[{config.name}] writing {len(rows):,} rows to {output_path}")
    with open(output_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ExportRow.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)
    _log(f"[{config.name}] export complete in {time.time() - started_at:.1f}s")
    return len(rows)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export normalized Uniswap v4 pool history CSV")
    parser.add_argument("--pool", choices=sorted(POOL_CONFIGS), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--end-block", type=int)
    parser.add_argument("--rpc-url")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    count = export_pool_history(
        POOL_CONFIGS[args.pool],
        output_path=args.output,
        start_block=args.start_block,
        end_block=args.end_block,
        rpc_url=args.rpc_url,
    )
    print(f"wrote {count} rows to {args.output}")
