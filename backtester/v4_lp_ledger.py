"""Pure helpers for reconstructing V4 LP lifecycle ledger rows."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from eth_abi import decode  # type: ignore[attr-defined]
from eth_abi.exceptions import DecodingError  # type: ignore[attr-defined]
from web3 import Web3

from backtester.clmm_math import cngn_price_from_sqrt_price_x96
from backtester.v4_event_replay import ReplayedEvent
from backtester.v4_export import ExportPoolConfig, POOL_CONFIGS, V4_MODIFY_LIQUIDITY_TOPIC
from engine.lp.types import (
    _V4_LP_BURN_POSITION,
    _V4_LP_DECREASE_LIQUIDITY,
    _V4_LP_INCREASE_LIQUIDITY,
    _V4_LP_MINT_POSITION,
    _V4_LP_TAKE_PAIR,
)
from engine.web3_utils import coerce_hex_str


TRANSFER_EVENT_TOPIC = coerce_hex_str(Web3.keccak(text="Transfer(address,address,uint256)").hex()).lower()


@dataclass(frozen=True)
class DecodedLiquidityAction:
    action_type: str
    block_number: int
    log_index: int
    event_order: int
    token_id: int
    lp_owner: str | None
    tick_lower: int | None
    tick_upper: int | None
    liquidity_delta: int
    amount0: Decimal | int | str
    amount1: Decimal | int | str
    collect_amount0: Decimal | int | str
    collect_amount1: Decimal | int | str = Decimal("0")
    chain: str = ""
    pool_id: str = ""
    block_time: str = ""
    tx_hash: str = ""
    position_manager: str = ""
    amount0_raw: str = ""
    amount1_raw: str = ""
    timestamp_ms: int | None = None


@dataclass(frozen=True)
class OwnershipEvent:
    block_number: int
    log_index: int
    token_id: int
    previous_owner: str | None
    new_owner: str | None
    event_order: int = 0


@dataclass(frozen=True)
class LedgerPositionState:
    pool_id: str
    tick_lower: int
    tick_upper: int
    liquidity_after: int


@dataclass(frozen=True)
class LPLedgerRow:
    chain: str
    pool_id: str
    block_number: int
    block_time: str
    tx_hash: str
    log_index: int
    event_order: int
    event_type: str
    position_manager: str
    token_id: int
    lp_owner: str | None
    owner_source: str
    tick_lower: int | None
    tick_upper: int | None
    liquidity_delta: int
    liquidity_after: int
    amount0: Decimal
    amount1: Decimal
    amount0_raw: str
    amount1_raw: str
    collect_amount0: Decimal
    collect_amount1: Decimal
    sqrt_price_x96_at_event: int
    tick_at_event: int
    cngn_usd_price_at_event: Decimal
    timestamp_ms: int


@dataclass(frozen=True)
class _TokenPositionState:
    tick_lower: int | None
    tick_upper: int | None
    liquidity_after: int


def decode_ownership_events_from_receipt(
    receipt: dict[str, Any],
    position_manager: str,
) -> list[OwnershipEvent]:
    events: list[OwnershipEvent] = []
    block_number = _int_from_rpc(receipt["blockNumber"])
    position_manager_address = Web3.to_checksum_address(position_manager)
    for log in receipt.get("logs", []):
        if Web3.to_checksum_address(log["address"]) != position_manager_address:
            continue
        topics = log.get("topics", [])
        if len(topics) < 4 or coerce_hex_str(topics[0]).lower() != TRANSFER_EVENT_TOPIC:
            continue
        previous_owner = _none_if_zero_address(_address_from_topic(topics[1]))
        new_owner = _none_if_zero_address(_address_from_topic(topics[2]))
        events.append(
            OwnershipEvent(
                block_number=block_number,
                log_index=_int_from_rpc(log["logIndex"]),
                token_id=int(coerce_hex_str(topics[3]), 16),
                previous_owner=previous_owner,
                new_owner=new_owner,
            )
        )
    return events


def decode_liquidity_actions_for_tx(
    tx: dict[str, Any],
    receipt: dict[str, Any],
    block_timestamp: int,
    config: ExportPoolConfig,
    token_state: dict[int, LedgerPositionState],
    *,
    position_resolver: Callable[[int, int], LedgerPositionState | None] | None = None,
) -> list[DecodedLiquidityAction]:
    actions, params = _decode_modify_liquidities_payload(tx["input"])
    if not actions:
        return []

    decoded_actions: list[DecodedLiquidityAction] = []
    block_number = _int_from_rpc(tx["blockNumber"])
    timestamp_ms = block_timestamp * 1000
    block_time = datetime.fromtimestamp(block_timestamp, tz=timezone.utc).isoformat()
    tx_hash = coerce_hex_str(tx["hash"])
    modify_log_indices = deque(_pool_modify_log_indices(receipt, config))
    tx_targets_pool = False
    active_token_id: int | None = None
    pending_negative_index: int | None = None

    try:
        for event_order, (action, raw) in enumerate(zip(actions, params)):
            action_code = _action_code(action)
            if action_code == _V4_LP_MINT_POSITION:
                (
                    pool_key,
                    tick_lower,
                    tick_upper,
                    liquidity_delta,
                    amount0_raw,
                    amount1_raw,
                    _recipient,
                ) = _decode_mint_param(raw)
                if not _pool_key_matches(pool_key, config):
                    continue
                token_id = _find_minted_token_id(receipt, config.position_manager)
                if token_id is None:
                    raise ValueError(f"target-pool mint missing ERC-721 token id in tx {tx_hash}")
                log_index = _next_modify_log_index(modify_log_indices, receipt)
                action_row = _decoded_action(
                    action_type="mint",
                    block_number=block_number,
                    log_index=log_index,
                    event_order=event_order,
                    token_id=token_id,
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    liquidity_delta=liquidity_delta,
                    amount0_raw=amount0_raw,
                    amount1_raw=amount1_raw,
                    collect_amount0=Decimal("0"),
                    collect_amount1=Decimal("0"),
                    block_time=block_time,
                    timestamp_ms=timestamp_ms,
                    tx_hash=tx_hash,
                    config=config,
                )
                decoded_actions.append(action_row)
                token_state[token_id] = LedgerPositionState(
                    pool_id=config.pool_id,
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    liquidity_after=liquidity_delta,
                )
                tx_targets_pool = True
                active_token_id = token_id
                pending_negative_index = None
                continue

            if action_code in {_V4_LP_INCREASE_LIQUIDITY, _V4_LP_DECREASE_LIQUIDITY}:
                token_id, liquidity_delta, amount0_raw, amount1_raw = _decode_increase_or_decrease_param(raw)
                position = token_state.get(token_id)
                if position is None and position_resolver is not None:
                    position = position_resolver(token_id, max(block_number - 1, 0))
                    if position is not None:
                        token_state[token_id] = position
                if position is None or position.pool_id.lower() != config.pool_id.lower():
                    continue
                event_type = "mint" if action_code == _V4_LP_INCREASE_LIQUIDITY else "burn"
                signed_delta = liquidity_delta if event_type == "mint" else -liquidity_delta
                amount0_for_action = amount0_raw if event_type == "mint" else 0
                amount1_for_action = amount1_raw if event_type == "mint" else 0
                log_index = _next_modify_log_index(modify_log_indices, receipt)
                action_row = _decoded_action(
                    action_type=event_type,
                    block_number=block_number,
                    log_index=log_index,
                    event_order=event_order,
                    token_id=token_id,
                    tick_lower=position.tick_lower,
                    tick_upper=position.tick_upper,
                    liquidity_delta=signed_delta,
                    amount0_raw=amount0_for_action,
                    amount1_raw=amount1_for_action,
                    collect_amount0=Decimal("0"),
                    collect_amount1=Decimal("0"),
                    block_time=block_time,
                    timestamp_ms=timestamp_ms,
                    tx_hash=tx_hash,
                    config=config,
                )
                decoded_actions.append(action_row)
                token_state[token_id] = replace(
                    position,
                    liquidity_after=max(position.liquidity_after + signed_delta, 0),
                )
                tx_targets_pool = True
                active_token_id = token_id
                pending_negative_index = len(decoded_actions) - 1 if signed_delta < 0 else None
                continue

            if action_code == _V4_LP_BURN_POSITION:
                token_id = _decode_burn_param(raw)
                position = token_state.get(token_id)
                if position is None and position_resolver is not None:
                    position = position_resolver(token_id, max(block_number - 1, 0))
                    if position is not None:
                        token_state[token_id] = position
                if position is None or position.pool_id.lower() != config.pool_id.lower():
                    continue
                tx_targets_pool = True
                active_token_id = token_id
                if position.liquidity_after > 0:
                    action_row = _decoded_action(
                        action_type="burn",
                        block_number=block_number,
                        log_index=_next_modify_log_index(modify_log_indices, receipt),
                        event_order=event_order,
                        token_id=token_id,
                        tick_lower=position.tick_lower,
                        tick_upper=position.tick_upper,
                        liquidity_delta=-position.liquidity_after,
                        amount0_raw=0,
                        amount1_raw=0,
                        collect_amount0=Decimal("0"),
                        collect_amount1=Decimal("0"),
                        block_time=block_time,
                        timestamp_ms=timestamp_ms,
                        tx_hash=tx_hash,
                        config=config,
                    )
                    decoded_actions.append(action_row)
                    pending_negative_index = len(decoded_actions) - 1
                token_state.pop(token_id, None)
                continue

            if action_code == _V4_LP_TAKE_PAIR:
                if not tx_targets_pool:
                    continue
                _currency0, _currency1, recipient = _decode_take_pair_param(raw)
                collect_amount0, collect_amount1 = _extract_take_pair_amounts(receipt, recipient, config)
                if pending_negative_index is not None:
                    decoded_actions[pending_negative_index] = replace(
                        decoded_actions[pending_negative_index],
                        action_type="burn_collect",
                        collect_amount0=collect_amount0,
                        collect_amount1=collect_amount1,
                    )
                    pending_negative_index = None
                    continue
                if active_token_id is None:
                    continue
                position = token_state.get(active_token_id)
                decoded_actions.append(
                    _decoded_action(
                        action_type="collect",
                        block_number=block_number,
                        log_index=_last_transfer_log_index_to_recipient(receipt, recipient, config),
                        event_order=event_order,
                        token_id=active_token_id,
                        tick_lower=position.tick_lower if position is not None else None,
                        tick_upper=position.tick_upper if position is not None else None,
                        liquidity_delta=0,
                        amount0_raw=0,
                        amount1_raw=0,
                        collect_amount0=collect_amount0,
                        collect_amount1=collect_amount1,
                        block_time=block_time,
                        timestamp_ms=timestamp_ms,
                        tx_hash=tx_hash,
                        config=config,
                    )
                )
    except DecodingError:
        return []
    return decoded_actions


def build_lp_ledger_rows(
    decoded_actions: Sequence[DecodedLiquidityAction],
    ownership_events: Sequence[OwnershipEvent],
    price_events: Sequence[ReplayedEvent],
) -> list[LPLedgerRow]:
    ownership_by_token = _ownership_by_token(ownership_events)
    price_by_event = {
        (event.block_number, event.log_index, event.event_order): event
        for event in price_events
    }
    token_state: dict[int, _TokenPositionState] = {}
    rows: list[LPLedgerRow] = []

    for action in sorted(decoded_actions, key=_action_sort_key):
        price_event = price_by_event.get((action.block_number, action.log_index, action.event_order))
        if price_event is None:
            raise ValueError(
                "missing event-time price for "
                f"{action.action_type} token_id={action.token_id} at "
                f"{action.block_number}:{action.log_index}:{action.event_order}"
            )

        previous_state = token_state.get(action.token_id)
        tick_lower = action.tick_lower
        tick_upper = action.tick_upper
        if previous_state is not None:
            tick_lower = previous_state.tick_lower if tick_lower is None else tick_lower
            tick_upper = previous_state.tick_upper if tick_upper is None else tick_upper
        liquidity_after = (
            previous_state.liquidity_after if previous_state is not None else 0
        ) + action.liquidity_delta
        if liquidity_after < 0:
            raise ValueError(f"liquidity_after below zero for token_id={action.token_id}")

        owner, owner_source = _owner_at_action(action, ownership_by_token.get(action.token_id, []))
        token_state[action.token_id] = _TokenPositionState(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity_after=liquidity_after,
        )
        rows.append(
            LPLedgerRow(
                chain=action.chain,
                pool_id=action.pool_id,
                block_number=action.block_number,
                block_time=action.block_time,
                tx_hash=action.tx_hash,
                log_index=action.log_index,
                event_order=action.event_order,
                event_type=action.action_type,
                position_manager=action.position_manager,
                token_id=action.token_id,
                lp_owner=owner,
                owner_source=owner_source,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                liquidity_delta=action.liquidity_delta,
                liquidity_after=liquidity_after,
                amount0=_decimal(action.amount0),
                amount1=_decimal(action.amount1),
                amount0_raw=action.amount0_raw,
                amount1_raw=action.amount1_raw,
                collect_amount0=_decimal(action.collect_amount0),
                collect_amount1=_decimal(action.collect_amount1),
                sqrt_price_x96_at_event=price_event.event_time_sqrt_price_x96,
                tick_at_event=price_event.event_time_tick,
                cngn_usd_price_at_event=_cngn_price_at_event(action, price_event),
                timestamp_ms=action.timestamp_ms if action.timestamp_ms is not None else action.block_number,
            )
        )

    return rows


def _ownership_by_token(ownership_events: Sequence[OwnershipEvent]) -> dict[int, list[OwnershipEvent]]:
    by_token: dict[int, list[OwnershipEvent]] = {}
    for event in ownership_events:
        by_token.setdefault(event.token_id, []).append(event)
    for events in by_token.values():
        events.sort(key=_ownership_sort_key)
    return by_token


def _owner_at_action(
    action: DecodedLiquidityAction,
    ownership_events: Sequence[OwnershipEvent],
) -> tuple[str | None, str]:
    if action.lp_owner is not None:
        return action.lp_owner, "action"
    owner: str | None = None
    for event in ownership_events:
        if _ownership_sort_key(event) <= _action_sort_key(action):
            owner = event.new_owner
            continue
        break
    if owner is None:
        return None, "unknown"
    return owner, "transfer"


def _cngn_price_at_event(
    action: DecodedLiquidityAction,
    price_event: ReplayedEvent,
) -> Decimal:
    config = _config_for_action(action)
    if config is None:
        raise ValueError(
            "cannot derive cNGN/USD price without known chain or pool_id "
            f"for token_id={action.token_id}"
        )
    return Decimal(str(cngn_price_from_sqrt_price_x96(
        price_event.event_time_sqrt_price_x96,
        config.token0_decimals,
        config.token1_decimals,
        config.invert_price,
    )))


def _config_for_action(action: DecodedLiquidityAction) -> ExportPoolConfig | None:
    if action.pool_id:
        for config in POOL_CONFIGS.values():
            if config.pool_id.lower() == action.pool_id.lower():
                return config
    if action.chain:
        for config in POOL_CONFIGS.values():
            if config.chain == action.chain:
                return config
    return None


def _decoded_action(
    *,
    action_type: str,
    block_number: int,
    log_index: int,
    event_order: int,
    token_id: int,
    tick_lower: int | None,
    tick_upper: int | None,
    liquidity_delta: int,
    amount0_raw: int,
    amount1_raw: int,
    collect_amount0: Decimal,
    collect_amount1: Decimal,
    block_time: str,
    timestamp_ms: int,
    tx_hash: str,
    config: ExportPoolConfig,
) -> DecodedLiquidityAction:
    return DecodedLiquidityAction(
        action_type=action_type,
        block_number=block_number,
        log_index=log_index,
        event_order=event_order,
        token_id=token_id,
        lp_owner=None,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
        amount0=_token_amount(amount0_raw, config.token0_decimals),
        amount1=_token_amount(amount1_raw, config.token1_decimals),
        amount0_raw=str(amount0_raw),
        amount1_raw=str(amount1_raw),
        collect_amount0=collect_amount0,
        collect_amount1=collect_amount1,
        chain=config.chain,
        pool_id=config.pool_id,
        block_time=block_time,
        tx_hash=tx_hash,
        position_manager=config.position_manager,
        timestamp_ms=timestamp_ms,
    )


def _decode_modify_liquidities_payload(input_data: str) -> tuple[bytes, list[bytes]]:
    raw = bytes.fromhex(coerce_hex_str(input_data)[10:])
    try:
        unlock_data, _deadline = decode(["bytes", "uint256"], raw)
        actions, params = decode(["bytes", "bytes[]"], unlock_data)
    except DecodingError:
        return b"", []
    return actions, list(params)


def _decode_mint_param(raw: bytes) -> tuple[tuple[Any, ...], int, int, int, int, int, str]:
    pool_key, tick_lower, tick_upper, liquidity, amount0_max, amount1_max, recipient, _hook = decode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        raw,
    )
    return (
        pool_key,
        int(tick_lower),
        int(tick_upper),
        int(liquidity),
        int(amount0_max),
        int(amount1_max),
        Web3.to_checksum_address(str(recipient)),
    )


def _decode_increase_or_decrease_param(raw: bytes) -> tuple[int, int, int, int]:
    token_id, liquidity_delta, amount0, amount1, _hook = decode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        raw,
    )
    return int(token_id), int(liquidity_delta), int(amount0), int(amount1)


def _decode_burn_param(raw: bytes) -> int:
    token_id, _amount0, _amount1, _hook = decode(["uint256", "uint128", "uint128", "bytes"], raw)
    return int(token_id)


def _decode_take_pair_param(raw: bytes) -> tuple[str, str, str]:
    currency0, currency1, recipient = decode(["address", "address", "address"], raw)
    return str(currency0), str(currency1), Web3.to_checksum_address(str(recipient))


def _pool_key_matches(pool_key: tuple[Any, ...], config: ExportPoolConfig) -> bool:
    return (
        Web3.to_checksum_address(pool_key[0]) == Web3.to_checksum_address(config.token0_address)
        and Web3.to_checksum_address(pool_key[1]) == Web3.to_checksum_address(config.token1_address)
        and int(pool_key[2]) == int(config.fee_rate * 1_000_000)
    )


def _pool_modify_log_indices(receipt: dict[str, Any], config: ExportPoolConfig) -> list[int]:
    indices: list[int] = []
    pool_manager = Web3.to_checksum_address(config.pool_manager)
    expected_pool_id = coerce_hex_str(config.pool_id).lower()
    for log in receipt.get("logs", []):
        if Web3.to_checksum_address(log["address"]) != pool_manager:
            continue
        topics = log.get("topics", [])
        if len(topics) < 2:
            continue
        if coerce_hex_str(topics[0]).lower() != coerce_hex_str(V4_MODIFY_LIQUIDITY_TOPIC).lower():
            continue
        if coerce_hex_str(topics[1]).lower() != expected_pool_id:
            continue
        indices.append(_int_from_rpc(log["logIndex"]))
    return sorted(indices)


def _next_modify_log_index(indices: deque[int], receipt: dict[str, Any]) -> int:
    if indices:
        return indices.popleft()
    if receipt.get("logs"):
        return _int_from_rpc(receipt["logs"][-1]["logIndex"])
    return 0


def _find_minted_token_id(receipt: dict[str, Any], position_manager: str) -> int | None:
    position_manager_address = Web3.to_checksum_address(position_manager)
    for log in receipt.get("logs", []):
        if Web3.to_checksum_address(log["address"]) != position_manager_address:
            continue
        topics = log.get("topics", [])
        if len(topics) < 4 or coerce_hex_str(topics[0]).lower() != TRANSFER_EVENT_TOPIC:
            continue
        if _none_if_zero_address(_address_from_topic(topics[1])) is not None:
            continue
        return int(coerce_hex_str(topics[3]), 16)
    return None


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
    for log in receipt.get("logs", []):
        if not _is_erc20_transfer_to(log, recipient):
            continue
        token_addr = Web3.to_checksum_address(log["address"])
        amount_raw = _int_from_rpc(log["data"])
        if token_addr == token0:
            amount0_raw += amount_raw
        elif token_addr == token1:
            amount1_raw += amount_raw
    return (
        _token_amount(amount0_raw, config.token0_decimals),
        _token_amount(amount1_raw, config.token1_decimals),
    )


def _last_transfer_log_index_to_recipient(
    receipt: dict[str, Any],
    recipient: str,
    config: ExportPoolConfig,
) -> int:
    token_addresses = {
        Web3.to_checksum_address(config.token0_address),
        Web3.to_checksum_address(config.token1_address),
    }
    recipient = Web3.to_checksum_address(recipient)
    last_index: int | None = None
    for log in receipt.get("logs", []):
        if Web3.to_checksum_address(log["address"]) not in token_addresses:
            continue
        if not _is_erc20_transfer_to(log, recipient):
            continue
        last_index = _int_from_rpc(log["logIndex"])
    return last_index if last_index is not None else _next_modify_log_index(deque(), receipt)


def _is_erc20_transfer_to(log: dict[str, Any], recipient: str) -> bool:
    topics = log.get("topics", [])
    if len(topics) < 3 or coerce_hex_str(topics[0]).lower() != TRANSFER_EVENT_TOPIC:
        return False
    return _address_from_topic(topics[2]) == Web3.to_checksum_address(recipient)


def _address_from_topic(topic: Any) -> str:
    topic_hex = coerce_hex_str(topic)
    return Web3.to_checksum_address("0x" + topic_hex[-40:])


def _none_if_zero_address(address: str) -> str | None:
    if int(address, 16) == 0:
        return None
    return address


def _token_amount(amount_raw: int, decimals: int) -> Decimal:
    return Decimal(amount_raw) / Decimal(10 ** decimals)


def _int_from_rpc(value: Any) -> int:
    if isinstance(value, str):
        if value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return int.from_bytes(bytes(value), "big")
    return int(value)


def _action_code(action: int | bytes) -> int:
    if isinstance(action, int):
        return action
    return action[0]


def _decimal(value: Decimal | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _action_sort_key(action: DecodedLiquidityAction) -> tuple[int, int, int]:
    return action.block_number, action.log_index, action.event_order


def _ownership_sort_key(event: OwnershipEvent) -> tuple[int, int, int]:
    return event.block_number, event.log_index, event.event_order
