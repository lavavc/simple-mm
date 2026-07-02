"""Historical Uniswap v4 event export for backtester datasets."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any

from eth_abi import decode  # type: ignore[attr-defined]
from eth_abi.exceptions import DecodingError  # type: ignore[attr-defined]
from requests import HTTPError
from web3 import Web3
from web3.middleware import geth_poa_middleware  # type: ignore[attr-defined]

from research.backtester.clmm_math import cngn_price_from_sqrt_price_x96
from research.backtester.v4_event_replay import PoolStateSnapshot, ReplayEvent, attach_event_time_state
from engine.config import settings
from engine.web3_utils import as_hexstr, coerce_hex_str

_V4_LP_INCREASE_LIQUIDITY = 0
_V4_LP_DECREASE_LIQUIDITY = 1
_V4_LP_MINT_POSITION = 2
_V4_LP_BURN_POSITION = 3
_V4_LP_TAKE_PAIR = 17
V4_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
V4_MODIFY_LIQUIDITY_TOPIC = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
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

_TRANSFER_EVENT_TOPIC = coerce_hex_str(Web3.keccak(text="Transfer(address,address,uint256)").hex()).lower()


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
    event_source: str = ""
    sender: str | None = None
    recipient: str | None = None
    currency0: str | None = None
    currency1: str | None = None
    hooks: str | None = None
    tick_spacing: int | None = None
    tick_lower: int | None = None
    tick_upper: int | None = None
    liquidity_delta: int | None = None
    salt: str | None = None
    amount0_raw: str | None = None
    amount1_raw: str | None = None


@dataclass(frozen=True)
class ResumeMetadata:
    row_count: int
    max_block: int | None
    initialize_found: bool


def _read_export_checkpoint(path: str | None, pool_name: str) -> int | None:
    if path is None or not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            payload = json.load(handle)
        if payload != {
            "pool": pool_name,
            "last_scanned_block": payload.get("last_scanned_block"),
        }:
            raise ValueError("checkpoint pool or fields do not match")
        block = payload["last_scanned_block"]
        if not isinstance(block, int) or block < 0:
            raise ValueError("checkpoint block must be a non-negative integer")
        return block
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid export checkpoint: {path}") from exc


def _write_export_checkpoint(path: str, pool_name: str, last_scanned_block: int) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    payload = {"pool": pool_name, "last_scanned_block": last_scanned_block}
    fd, temporary_path = tempfile.mkstemp(prefix=".pool-history-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise


def _resolve_resume_start_block(
    requested_start_block: int,
    metadata: ResumeMetadata,
    checkpoint_block: int | None,
) -> int:
    durable_blocks = [requested_start_block - 1]
    if metadata.max_block is not None:
        durable_blocks.append(metadata.max_block)
    if checkpoint_block is not None:
        durable_blocks.append(checkpoint_block)
    return max(durable_blocks) + 1


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
        default_start_block=42_926_879,
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
        default_start_block=84_655_203,
        chunk_size=5_000,
    ),
}


def _make_web3(config: ExportPoolConfig) -> Web3:
    w3 = Web3(Web3.HTTPProvider(config.rpc_url))
    if config.chain == "bsc":
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
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


def _candidate_modify_liquidity_tx_hashes(
    w3: Web3,
    config: ExportPoolConfig,
    from_block: int,
    to_block: int,
) -> list[str]:
    tx_hashes: set[str] = set()
    for chunk_start in range(from_block, to_block + 1, config.chunk_size):
        chunk_end = min(chunk_start + config.chunk_size - 1, to_block)
        logs = _fetch_logs_with_debug(
            w3,
            {
                "address": Web3.to_checksum_address(config.position_manager),
                "fromBlock": chunk_start,
                "toBlock": chunk_end,
            },
            context=f"[{config.name}] position manager logs {chunk_start:,}->{chunk_end:,}",
        )
        tx_hashes.update(coerce_hex_str(log["transactionHash"]) for log in logs if log.get("transactionHash"))
    return sorted(tx_hashes)


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
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "big")
    return int(value)


def _int128_from_word(word: bytes) -> int:
    return int.from_bytes(word, "big", signed=True)


def _word_int(data_bytes: bytes, index: int, *, signed: bool = False) -> int:
    return int.from_bytes(data_bytes[index * 32:(index + 1) * 32], "big", signed=signed)


def _word_hex(data_bytes: bytes, index: int) -> str:
    return "0x" + data_bytes[index * 32:(index + 1) * 32].hex()


def _datetime_from_block_ts(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _stable_and_cngn_amounts(amount0: Decimal, amount1: Decimal, config: ExportPoolConfig) -> tuple[Decimal, Decimal]:
    if config.token0_symbol == "cNGN":
        return abs(amount1), abs(amount0)
    return abs(amount0), abs(amount1)


def derive_cngn_price(amount0_raw: int, amount1_raw: int, config: ExportPoolConfig) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        amount0 = Decimal(abs(amount0_raw)) / Decimal(10 ** config.token0_decimals)
        amount1 = Decimal(abs(amount1_raw)) / Decimal(10 ** config.token1_decimals)
        stable_amount, cngn_amount = _stable_and_cngn_amounts(amount0, amount1, config)
        if cngn_amount <= 0:
            return Decimal("0")
        return +(stable_amount / cngn_amount)


def _cngn_price_from_pool_state(sqrt_price_x96: int, config: ExportPoolConfig) -> float:
    return cngn_price_from_sqrt_price_x96(
        sqrt_price_x96,
        config.token0_decimals,
        config.token1_decimals,
        config.invert_price,
    )


def decode_swap_row(log: dict[str, Any], block_timestamp: int, config: ExportPoolConfig) -> ExportRow:
    data_hex = coerce_hex_str(log["data"])
    data_bytes = bytes.fromhex(data_hex[2:])
    amount0_raw = _int128_from_word(data_bytes[0:32])
    amount1_raw = _int128_from_word(data_bytes[32:64])
    sqrt_price_x96 = int.from_bytes(data_bytes[64:96], "big")
    active_liquidity = int.from_bytes(data_bytes[96:128], "big")
    tick = int.from_bytes(data_bytes[128:160], "big", signed=True)
    fee_word = int.from_bytes(data_bytes[160:192], "big")
    cngn_price = _cngn_price_from_pool_state(sqrt_price_x96, config)
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
        cngn_usd_price=cngn_price,
        token0_symbol=config.token0_symbol,
        token1_symbol=config.token1_symbol,
        event_source="pool_manager_swap",
        sender=_address_from_topic(log["topics"][2]) if len(log.get("topics", [])) > 2 else None,
        amount0_raw=str(amount0_raw),
        amount1_raw=str(amount1_raw),
    )


def decode_initialize_row(log: dict[str, Any], block_timestamp: int, config: ExportPoolConfig) -> ExportRow:
    data_hex = coerce_hex_str(log["data"])
    data_bytes = bytes.fromhex(data_hex[2:])
    fee = _word_int(data_bytes, 0)
    tick_spacing = _word_int(data_bytes, 1, signed=True)
    hooks = Web3.to_checksum_address("0x" + _word_hex(data_bytes, 2)[-40:])
    sqrt_price_x96 = _word_int(data_bytes, 3)
    tick = _word_int(data_bytes, 4, signed=True)
    cngn_price = _cngn_price_from_pool_state(sqrt_price_x96, config)
    topics = log.get("topics", [])
    return ExportRow(
        block_time=_datetime_from_block_ts(block_timestamp),
        chain=config.chain,
        pool_id=config.pool_id,
        event_type="initialize",
        tx_hash=coerce_hex_str(log["transactionHash"]),
        log_index=int(log["logIndex"]),
        block_number=int(log["blockNumber"]),
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        active_liquidity=0,
        fee_rate=fee / 1_000_000,
        amount0=0.0,
        amount1=0.0,
        amount_usd=0.0,
        cngn_usd_price=cngn_price,
        token0_symbol=config.token0_symbol,
        token1_symbol=config.token1_symbol,
        event_source="pool_manager_initialize",
        currency0=_address_from_topic(topics[2]) if len(topics) > 2 else None,
        currency1=_address_from_topic(topics[3]) if len(topics) > 3 else None,
        hooks=hooks,
        tick_spacing=tick_spacing,
    )


def decode_modify_liquidity_row(
    log: dict[str, Any],
    block_timestamp: int,
    pool_state: tuple[int, int, int, float],
    config: ExportPoolConfig,
) -> ExportRow:
    data_hex = coerce_hex_str(log["data"])
    data_bytes = bytes.fromhex(data_hex[2:])
    tick_lower = _word_int(data_bytes, 0, signed=True)
    tick_upper = _word_int(data_bytes, 1, signed=True)
    liquidity_delta = _word_int(data_bytes, 2, signed=True)
    salt = _word_hex(data_bytes, 3)
    sqrt_price_x96, tick, active_liquidity, cngn_usd_price = pool_state
    if liquidity_delta > 0:
        event_type = "mint"
    elif liquidity_delta < 0:
        event_type = "burn"
    else:
        event_type = "collect"
    topics = log.get("topics", [])
    return ExportRow(
        block_time=_datetime_from_block_ts(block_timestamp),
        chain=config.chain,
        pool_id=config.pool_id,
        event_type=event_type,
        tx_hash=coerce_hex_str(log["transactionHash"]),
        log_index=int(log["logIndex"]),
        block_number=int(log["blockNumber"]),
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        active_liquidity=active_liquidity,
        fee_rate=config.fee_rate,
        amount0=0.0,
        amount1=0.0,
        amount_usd=0.0,
        cngn_usd_price=cngn_usd_price,
        token0_symbol=config.token0_symbol,
        token1_symbol=config.token1_symbol,
        event_source="pool_manager_modify_liquidity",
        sender=_address_from_topic(topics[2]) if len(topics) > 2 else None,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
        salt=salt,
    )


def _pool_key_matches(pool_key: tuple[Any, ...], config: ExportPoolConfig) -> bool:
    return (
        Web3.to_checksum_address(pool_key[0]) == Web3.to_checksum_address(config.token0_address)
        and Web3.to_checksum_address(pool_key[1]) == Web3.to_checksum_address(config.token1_address)
        and int(pool_key[2]) == int(config.fee_rate * 1_000_000)
    )


def decode_modify_liquidities_payload(input_data: str) -> tuple[bytes, list[bytes]]:
    raw = bytes.fromhex(coerce_hex_str(input_data)[10:])
    try:
        unlock_data, _deadline = decode(["bytes", "uint256"], raw)
        actions, params = decode(["bytes", "bytes[]"], unlock_data)
    except DecodingError:
        return b"", []
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


def _decode_take_pair_param(raw: bytes) -> tuple[str, str, str]:
    currency0, currency1, recipient = decode(["address", "address", "address"], raw)
    return str(currency0), str(currency1), Web3.to_checksum_address(str(recipient))


def _address_from_topic(topic: Any) -> str:
    topic_hex = coerce_hex_str(topic)
    return Web3.to_checksum_address("0x" + topic_hex[-40:])


def _extract_take_pair_amounts(
    receipt: dict[str, Any],
    recipient: str,
    config: ExportPoolConfig,
) -> tuple[Decimal, Decimal]:
    amount0_raw = 0
    amount1_raw = 0
    token0 = Web3.to_checksum_address(config.token0_address)
    token1 = Web3.to_checksum_address(config.token1_address)
    recipient = Web3.to_checksum_address(recipient)

    for log in receipt["logs"]:
        if len(log["topics"]) < 3:
            continue
        if coerce_hex_str(log["topics"][0]).lower() != _TRANSFER_EVENT_TOPIC:
            continue
        if _address_from_topic(log["topics"][2]) != recipient:
            continue
        token_addr = Web3.to_checksum_address(log["address"])
        amount_raw = _int_from_rpc(log["data"])
        if token_addr == token0:
            amount0_raw += amount_raw
        elif token_addr == token1:
            amount1_raw += amount_raw

    amount0 = Decimal(amount0_raw) / Decimal(10 ** config.token0_decimals)
    amount1 = Decimal(amount1_raw) / Decimal(10 ** config.token1_decimals)
    return amount0, amount1


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


def _state_at_block(state_view: Any, config: ExportPoolConfig, block_number: int) -> tuple[int, int, int, float]:
    pool_id_bytes = bytes.fromhex(config.pool_id[2:])
    slot0 = state_view.functions.getSlot0(pool_id_bytes).call(block_identifier=block_number)
    sqrt_price_x96 = int(slot0[0])
    tick = int(slot0[1])
    liquidity = int(state_view.functions.getLiquidity(pool_id_bytes).call(block_identifier=block_number))
    cngn_price = _cngn_price_from_pool_state(sqrt_price_x96, config)
    return sqrt_price_x96, tick, liquidity, cngn_price


def _event_time_seed_required(rows: list[ExportRow]) -> bool:
    state_known = False
    for row in sorted(rows, key=lambda value: (value.block_number, value.log_index)):
        if row.event_type in {"initialize", "swap"}:
            state_known = True
        elif row.event_type in {"mint", "burn", "collect"}:
            if not state_known:
                return True
        else:
            raise ValueError(f"unsupported export event type: {row.event_type}")
    return False


def _state_seed_before_block(
    state_view: Any,
    config: ExportPoolConfig,
    block_number: int,
) -> PoolStateSnapshot | None:
    if block_number <= config.default_start_block:
        return None
    sqrt_price_x96, tick, _active_liquidity, _cngn_usd_price = _state_at_block(
        state_view,
        config,
        block_number - 1,
    )
    return PoolStateSnapshot(sqrt_price_x96=sqrt_price_x96, tick=tick, source="prior_block")


def _apply_event_time_price_replay(
    rows: list[ExportRow],
    initial_state: PoolStateSnapshot | None,
    config: ExportPoolConfig,
) -> list[ExportRow]:
    sorted_rows = sorted(rows, key=lambda value: (value.block_number, value.log_index))
    replay_events = [
        ReplayEvent(
            block_number=row.block_number,
            log_index=row.log_index,
            event_order=event_order,
            event_type=row.event_type,
            sqrt_price_x96=row.sqrt_price_x96 if row.event_type in {"initialize", "swap"} else None,
            tick=row.tick if row.event_type in {"initialize", "swap"} else None,
        )
        for event_order, row in enumerate(sorted_rows)
    ]
    replayed_events = attach_event_time_state(replay_events, initial_state)
    replayed_rows: list[ExportRow] = []
    for row, replayed_event in zip(sorted_rows, replayed_events):
        if row.event_type in {"initialize", "swap"}:
            replayed_rows.append(row)
            continue
        replayed_rows.append(
            replace(
                row,
                sqrt_price_x96=replayed_event.event_time_sqrt_price_x96,
                tick=replayed_event.event_time_tick,
                cngn_usd_price=_cngn_price_from_pool_state(replayed_event.event_time_sqrt_price_x96, config),
            )
        )
    return replayed_rows


def _find_minted_token_id(receipt: dict[str, Any], position_manager: str) -> int | None:
    for log in receipt["logs"]:
        if Web3.to_checksum_address(log["address"]) != Web3.to_checksum_address(position_manager):
            continue
        topics = [coerce_hex_str(topic).lower() for topic in log["topics"]]
        if not topics or topics[0] != _TRANSFER_EVENT_TOPIC:
            continue
        return int(coerce_hex_str(topics[3]), 16)
    return None


def _token_id_targets_pool(
    token_id: int,
    token_state: dict[int, PositionTokenState],
    position_manager: Any,
    config: ExportPoolConfig,
) -> bool:
    position = token_state.get(token_id)
    if position is not None:
        return position.pool_id == config.pool_id
    return _position_state_from_chain(token_id, position_manager, config) is not None


def _signed_int24(value: int) -> int:
    value &= (1 << 24) - 1
    if value >= 1 << 23:
        value -= 1 << 24
    return value


def _int_from_position_info(info: Any) -> int:
    if isinstance(info, int):
        return info
    if isinstance(info, (bytes, bytearray)):
        return int.from_bytes(info, "big")
    return int(coerce_hex_str(info), 16)


def _decode_position_info(info: Any) -> tuple[str, int, int]:
    """Decode v4 periphery PositionInfo.

    PositionInfo packs:
    200 bits poolId | 24 bits tickUpper | 24 bits tickLower | 8 bits hasSubscriber.
    """
    value = _int_from_position_info(info)
    tick_lower = _signed_int24(value >> 8)
    tick_upper = _signed_int24(value >> 32)
    pool_prefix = value >> 56
    return f"0x{pool_prefix:050x}", tick_lower, tick_upper


def _pool_id_prefix_matches(pool_prefix: str, config: ExportPoolConfig) -> bool:
    expected = int(config.pool_id, 16) >> 56
    return int(pool_prefix, 16) == expected


def _read_existing_export_metadata(output_path: str) -> ResumeMetadata:
    row_count = 0
    max_block: int | None = None
    initialize_found = False
    try:
        handle = open(output_path, newline="")
    except FileNotFoundError:
        return ResumeMetadata(row_count=0, max_block=None, initialize_found=False)

    with handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_count += 1
            if row.get("event_type") == "initialize":
                initialize_found = True
            block_number = row.get("block_number")
            if block_number in (None, ""):
                continue
            try:
                block_int = int(block_number)
            except ValueError:
                continue
            max_block = block_int if max_block is None else max(max_block, block_int)
    return ResumeMetadata(row_count=row_count, max_block=max_block, initialize_found=initialize_found)


def _position_state_from_chain(
    token_id: int,
    position_manager: Any,
    config: ExportPoolConfig,
    block_number: int | None = None,
) -> PositionTokenState | None:
    call_kwargs = {"block_identifier": block_number} if block_number is not None and block_number >= 0 else {}
    try:
        pool_key, info = position_manager.functions.getPoolAndPositionInfo(token_id).call(**call_kwargs)
    except Exception:
        return None
    if not _pool_key_matches(pool_key, config):
        return None
    pool_prefix, tick_lower, tick_upper = _decode_position_info(info)
    if not _pool_id_prefix_matches(pool_prefix, config):
        return None
    try:
        liquidity = int(position_manager.functions.getPositionLiquidity(token_id).call(**call_kwargs))
    except Exception:
        liquidity = 0
    return PositionTokenState(
        pool_id=config.pool_id,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=liquidity,
    )


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
    position_manager: Any,
    config: ExportPoolConfig,
    token_state: dict[int, PositionTokenState],
) -> list[ExportRow]:
    rows: list[ExportRow] = []
    actions, params = decode_modify_liquidities_payload(tx["input"])
    if not actions:
        return rows
    block_number = _int_from_rpc(tx["blockNumber"])
    resolve_block = max(block_number - 1, 0)
    sqrt_price_x96, tick, active_liquidity, cngn_usd_price = _state_at_block(
        state_view, config, block_number
    )
    tx_targets_pool = False

    try:
        for action, raw in zip(actions, params):
            action_code = action if isinstance(action, int) else action
            if isinstance(action_code, bytes):
                action_code = action_code[0]
            if action_code == _V4_LP_MINT_POSITION:
                pool_key, tick_lower, tick_upper, liquidity_delta, amount0_max, amount1_max = _decode_mint_param(raw)
                if not _pool_key_matches(pool_key, config):
                    continue
                tx_targets_pool = True
                token_id = _find_minted_token_id(receipt, config.position_manager)
                if token_id is not None:
                    token_state[token_id] = PositionTokenState(
                        pool_id=config.pool_id,
                        tick_lower=tick_lower,
                        tick_upper=tick_upper,
                        liquidity=liquidity_delta,
                    )
            elif action_code in {_V4_LP_INCREASE_LIQUIDITY, _V4_LP_DECREASE_LIQUIDITY}:
                token_id, liquidity_delta = _decode_increase_or_decrease_param(raw)
                position = token_state.get(token_id)
                if position is None:
                    position = _position_state_from_chain(token_id, position_manager, config, resolve_block)
                    if position is not None:
                        token_state[token_id] = position
                if position is None or position.pool_id != config.pool_id:
                    continue
                tx_targets_pool = True
                event_type = "mint" if action_code == _V4_LP_INCREASE_LIQUIDITY else "burn"
                new_liquidity = position.liquidity + liquidity_delta if event_type == "mint" else max(position.liquidity - liquidity_delta, 0)
                token_state[token_id] = PositionTokenState(
                    pool_id=position.pool_id,
                    tick_lower=position.tick_lower,
                    tick_upper=position.tick_upper,
                    liquidity=new_liquidity,
                )
            elif action_code == _V4_LP_BURN_POSITION:
                token_id = _decode_burn_param(raw)
                position = token_state.get(token_id)
                if position is None:
                    position = _position_state_from_chain(token_id, position_manager, config, resolve_block)
                    if position is not None:
                        token_state[token_id] = position
                if position is not None and position.pool_id == config.pool_id:
                    tx_targets_pool = True
                token_state.pop(token_id, None)
            elif action_code == _V4_LP_TAKE_PAIR:
                if not tx_targets_pool:
                    continue
                _currency0, _currency1, recipient = _decode_take_pair_param(raw)
                amount0, amount1 = _extract_take_pair_amounts(receipt, recipient, config)
                stable_amount, _ = _stable_and_cngn_amounts(amount0, amount1, config)
                rows.append(
                    ExportRow(
                        block_time=_datetime_from_block_ts(block_timestamp),
                        chain=config.chain,
                        pool_id=config.pool_id,
                        event_type="collect",
                        tx_hash=coerce_hex_str(tx["hash"]),
                        log_index=int(receipt["logs"][-1]["logIndex"]) if receipt["logs"] else 0,
                        block_number=block_number,
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
                        event_source="position_manager_take_pair",
                        recipient=recipient,
                    )
                )
    except DecodingError:
        return []
    return rows


def export_pool_history(
    config: ExportPoolConfig,
    output_path: str,
    start_block: int | None,
    end_block: int | None = None,
    rpc_url: str | None = None,
    resume: bool = False,
    checkpoint_path: str | None = None,
) -> int:
    if checkpoint_path is not None and not resume:
        raise ValueError("checkpoint_path requires resume=True")
    if rpc_url is not None:
        config = ExportPoolConfig(**{**config.__dict__, "rpc_url": rpc_url})
    started_at = time.time()
    w3 = _make_web3(config)
    state_view = w3.eth.contract(address=Web3.to_checksum_address(config.state_view), abi=STATE_VIEW_ABI)
    position_manager = w3.eth.contract(address=Web3.to_checksum_address(config.position_manager), abi=POSITION_MANAGER_ABI)
    latest_block = int(w3.eth.block_number)
    resume_metadata = _read_existing_export_metadata(output_path) if resume else ResumeMetadata(0, None, False)
    checkpoint_block = _read_export_checkpoint(checkpoint_path, config.name) if resume else None
    requested_start_block = config.default_start_block if start_block is None else start_block
    if resume:
        start_block = _resolve_resume_start_block(requested_start_block, resume_metadata, checkpoint_block)
    else:
        start_block = requested_start_block
    end_block = latest_block if end_block is None else end_block
    if resume:
        _log(
            f"[{config.name}] resume enabled: existing_rows={resume_metadata.row_count:,}, "
            f"existing_max_block={resume_metadata.max_block}, start={start_block:,}"
        )
    _log(
        f"[{config.name}] export start: blocks {start_block:,} -> {end_block:,}, "
        f"initial chunk={config.chunk_size:,}, rpc={config.rpc_url}"
    )
    block_timestamps: dict[int, int] = {}
    pool_state_cache: dict[int, tuple[int, int, int, float]] = {}
    token_state: dict[int, PositionTokenState] = {}
    total_rows = resume_metadata.row_count

    chunk_start = start_block
    current_chunk_size = config.chunk_size
    chunk_index = 0
    receipt_workers = 16
    modify_selector = coerce_hex_str(position_manager.functions.modifyLiquidities(b"", 0).selector)
    initialize_found = resume_metadata.initialize_found or start_block > config.default_start_block
    if start_block > end_block:
        _log(f"[{config.name}] export complete: output is already current through requested end block")
        return total_rows
    write_header = not resume or resume_metadata.row_count == 0
    output_mode = "a" if resume and resume_metadata.row_count > 0 else "w"
    with open(output_path, output_mode, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ExportRow.__dataclass_fields__.keys()))
        if write_header:
            writer.writeheader()
        handle.flush()

        while chunk_start <= end_block:
            chunk_rows: list[ExportRow] = []
            chunk_end = min(chunk_start + current_chunk_size - 1, end_block)
            chunk_index += 1
            if not initialize_found:
                _log(
                    f"[{config.name}] chunk {chunk_index}: scanning initialize logs for "
                    f"{chunk_start:,} -> {chunk_end:,}"
                )
                initialize_logs = _fetch_logs_with_debug(
                    w3,
                    {
                        "address": Web3.to_checksum_address(config.pool_manager),
                        "topics": [as_hexstr(V4_INITIALIZE_TOPIC), as_hexstr(config.pool_id)],
                        "fromBlock": chunk_start,
                        "toBlock": chunk_end,
                    },
                    context=f"[{config.name}] initialize logs {chunk_start:,}->{chunk_end:,}",
                )
                for log in initialize_logs:
                    block_number = int(log["blockNumber"])
                    if block_number not in block_timestamps:
                        raw_block = _raw_get_block(w3, block_number, False)
                        block_timestamps[block_number] = _block_timestamp_from_raw(raw_block)
                    chunk_rows.append(decode_initialize_row(log, block_timestamps[block_number], config))
                initialize_found = bool(initialize_logs)

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
                    chunk_index -= 1
                    continue
                raise
            _log(f"[{config.name}] chunk {chunk_index}: fetched {len(swap_logs):,} swap logs")
            for log in swap_logs:
                block_number = int(log["blockNumber"])
                if block_number not in block_timestamps:
                    raw_block = _raw_get_block(w3, block_number, False)
                    block_timestamps[block_number] = _block_timestamp_from_raw(raw_block)
                chunk_rows.append(decode_swap_row(log, block_timestamps[block_number], config))

            _log(
                f"[{config.name}] chunk {chunk_index}: scanning modify liquidity logs for "
                f"{chunk_start:,} -> {chunk_end:,}"
            )
            modify_logs = _fetch_logs_with_debug(
                w3,
                {
                    "address": Web3.to_checksum_address(config.pool_manager),
                    "topics": [as_hexstr(V4_MODIFY_LIQUIDITY_TOPIC), as_hexstr(config.pool_id)],
                    "fromBlock": chunk_start,
                    "toBlock": chunk_end,
                },
                context=f"[{config.name}] modify liquidity logs {chunk_start:,}->{chunk_end:,}",
            )
            _log(f"[{config.name}] chunk {chunk_index}: fetched {len(modify_logs):,} modify liquidity logs")
            for log in modify_logs:
                block_number = int(log["blockNumber"])
                if block_number not in block_timestamps:
                    raw_block = _raw_get_block(w3, block_number, False)
                    block_timestamps[block_number] = _block_timestamp_from_raw(raw_block)
                if block_number not in pool_state_cache:
                    pool_state_cache[block_number] = _state_at_block(state_view, config, block_number)
                chunk_rows.append(
                    decode_modify_liquidity_row(
                        log,
                        block_timestamps[block_number],
                        pool_state_cache[block_number],
                        config,
                    )
                )

            _log(
                f"[{config.name}] chunk {chunk_index}: scanning position manager logs for modifyLiquidities "
                f"{chunk_start:,} -> {chunk_end:,}"
            )
            candidate_tx_hashes = _candidate_modify_liquidity_tx_hashes(w3, config, chunk_start, chunk_end)
            _log(
                f"[{config.name}] chunk {chunk_index}: found {len(candidate_tx_hashes):,} candidate tx hashes, "
                f"rows={total_rows + len(chunk_rows):,}, tracked_positions={len(token_state):,}"
            )
            tx_matches = 0
            with ThreadPoolExecutor(max_workers=receipt_workers) as pool:
                txs = list(pool.map(lambda tx_hash: w3.eth.get_transaction(tx_hash), candidate_tx_hashes))
                matched_txs = [
                    tx for tx in txs
                    if tx.get("input")
                    and coerce_hex_str(tx["input"]).startswith(modify_selector)
                ]
                tx_matches = len(matched_txs)
                receipts = list(pool.map(lambda tx: w3.eth.get_transaction_receipt(tx["hash"]), matched_txs))
                for tx, receipt in zip(matched_txs, receipts):
                    block_number = _int_from_rpc(tx["blockNumber"])
                    if block_number not in block_timestamps:
                        raw_block = _raw_get_block(w3, block_number, False)
                        block_timestamps[block_number] = _block_timestamp_from_raw(raw_block)
                    chunk_rows.extend(
                        build_liquidity_rows_for_tx(
                            tx,
                            receipt,
                            block_timestamps[block_number],
                            state_view,
                            position_manager,
                            config,
                            token_state,
                        )
                    )

            event_time_seed = (
                _state_seed_before_block(state_view, config, chunk_start)
                if _event_time_seed_required(chunk_rows)
                else None
            )
            chunk_rows = _apply_event_time_price_replay(chunk_rows, event_time_seed, config)
            chunk_rows.sort(key=lambda row: (row.block_time, row.block_number, row.log_index))
            for row in chunk_rows:
                writer.writerow(row.__dict__)
            handle.flush()
            os.fsync(handle.fileno())
            total_rows += len(chunk_rows)
            if checkpoint_path is not None:
                _write_export_checkpoint(checkpoint_path, config.name, chunk_end)
            elapsed = time.time() - started_at
            _log(
                f"[{config.name}] chunk {chunk_index}: flushed {len(chunk_rows):,} rows. "
                f"modifyLiquidities txs={tx_matches:,}, total_rows={total_rows:,}, elapsed={elapsed:.1f}s"
            )
            chunk_start = chunk_end + 1
    _log(f"[{config.name}] export complete in {time.time() - started_at:.1f}s")
    return total_rows


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export normalized Uniswap v4 pool history CSV")
    parser.add_argument("--pool", choices=sorted(POOL_CONFIGS), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--end-block", type=int)
    parser.add_argument("--rpc-url")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Append to an existing CSV and start after the highest exported block.",
    )
    parser.add_argument(
        "--checkpoint-file",
        help="Persist the last fully flushed block so quiet ranges resume without rescanning.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    count = export_pool_history(
        POOL_CONFIGS[args.pool],
        output_path=args.output,
        start_block=args.start_block,
        end_block=args.end_block,
        rpc_url=args.rpc_url,
        resume=args.resume,
        checkpoint_path=args.checkpoint_file,
    )
    print(f"wrote {count} rows to {args.output}")
