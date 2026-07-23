"""Pure helpers for reconstructing V4 LP lifecycle ledger rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError
from web3 import Web3

from engine.config import settings
from engine.lp.types import (
    _V4_LP_BURN_POSITION,
    _V4_LP_DECREASE_LIQUIDITY,
    _V4_LP_INCREASE_LIQUIDITY,
    _V4_LP_MINT_POSITION,
    _V4_LP_SETTLE,
    _V4_LP_SETTLE_PAIR,
    _V4_LP_TAKE_PAIR,
)
from engine.web3_utils import coerce_hex_str
from research.backtester.clmm_math import cngn_price_from_sqrt_price_x96
from research.backtester.v4_event_replay import ReplayedEvent
from research.backtester.v4_export import POOL_CONFIGS, V4_MODIFY_LIQUIDITY_TOPIC, ExportPoolConfig

TRANSFER_EVENT_TOPIC = coerce_hex_str(Web3.keccak(text="Transfer(address,address,uint256)").hex()).lower()
MODIFY_LIQUIDITIES_SELECTOR = coerce_hex_str(Web3.keccak(text="modifyLiquidities(bytes,uint256)")[:4].hex()).lower()
MULTICALL_SELECTOR = coerce_hex_str(Web3.keccak(text="multicall(bytes[])")[:4].hex()).lower()
HANDLE_OPS_SELECTOR = coerce_hex_str(
    Web3.keccak(
        text=(
            "handleOps((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[],"
            "address)"
        )
    )[:4].hex()
).lower()
ACCOUNT_EXECUTE_SELECTOR = coerce_hex_str(
    Web3.keccak(text="execute(bytes32,bytes)")[:4].hex()
).lower()
_PACKED_USER_OPERATIONS_TYPE = (
    "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[]"
)
_BATCH_EXECUTION_MODE = b"\x01" + b"\x00" * 31
OPENING_ATTRIBUTION_EXACT = "exact"
OPENING_ATTRIBUTION_PENDING = "pending_receipt_attribution"
OPENING_ATTRIBUTION_NOT_APPLICABLE = "not_applicable"
OPENING_ATTRIBUTION_AMBIGUOUS_MULTIPLE = "ambiguous_multiple_add_actions"
OPENING_ATTRIBUTION_AMBIGUOUS_MISSING_SETTLE = "ambiguous_missing_settle_pair"
OPENING_ATTRIBUTION_AMBIGUOUS_NO_TRANSFER = "ambiguous_no_settlement_transfer"
OPENING_ATTRIBUTION_SOURCE_TRANSFER = "erc20_transfer_to_settlement"
OPENING_ATTRIBUTION_SOURCE_AMBIGUOUS = "ambiguous"
OPENING_ATTRIBUTION_SOURCE_NOT_APPLICABLE = "not_applicable"
ACTION_RECIPIENT_MSG_SENDER = Web3.to_checksum_address("0x0000000000000000000000000000000000000001")
ACTION_RECIPIENT_ADDRESS_THIS = Web3.to_checksum_address("0x0000000000000000000000000000000000000002")
ENTRYPOINT_V08_ADDRESS = "0x4337084d9e255ff0702461cf8895ce9e3b5ff108"


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
    amount0_actual: Decimal | int | str | None = None
    amount1_actual: Decimal | int | str | None = None
    amount0_attribution_source: str = ""
    amount1_attribution_source: str = ""
    amount_attribution_status: str = ""


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
class PoolPositionKey:
    pool_id: str
    tick_lower: int
    tick_upper: int
    salt: str


@dataclass(frozen=True)
class PositionManagerCallContext:
    input_data: str
    effective_sender: str
    wrapper_source: str
    user_operation_index: int | None
    batch_index: int | None
    outer_transaction_hash: str


@dataclass(frozen=True)
class _ModifyLiquidityWitness:
    log_index: int
    pool_id: str
    sender: str
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    salt: str


@dataclass(frozen=True)
class _MintTransferWitness:
    log_index: int
    token_id: int
    recipient: str


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
    amount0_actual: Decimal
    amount1_actual: Decimal
    amount0_attribution_source: str
    amount1_attribution_source: str
    amount_attribution_status: str
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


def position_manager_calls_for_transaction(
    tx: dict[str, Any],
    position_manager: str,
    wrapper_entrypoint: str,
) -> tuple[PositionManagerCallContext, ...]:
    position_manager_address = Web3.to_checksum_address(position_manager)
    entrypoint_address = Web3.to_checksum_address(wrapper_entrypoint)
    outer_target = _required_checksum_address(tx.get("to"), "transaction target")
    outer_sender = _required_checksum_address(tx.get("from"), "transaction sender")
    transaction_hash = coerce_hex_str(tx.get("hash")).lower()
    input_data = _canonical_calldata(tx.get("input"), "transaction input")
    selector = input_data[:10]

    if outer_target == position_manager_address:
        return (
            PositionManagerCallContext(
                input_data=input_data,
                effective_sender=outer_sender,
                wrapper_source="direct",
                user_operation_index=None,
                batch_index=None,
                outer_transaction_hash=transaction_hash,
            ),
        )
    if selector == HANDLE_OPS_SELECTOR and outer_target != entrypoint_address:
        raise ValueError("handleOps transaction targets the wrong EntryPoint")
    if outer_target != entrypoint_address:
        return ()
    if selector != HANDLE_OPS_SELECTOR:
        raise ValueError("configured EntryPoint transaction has an unsupported selector")

    user_operations, _beneficiary = _decode_canonical_abi(
        [_PACKED_USER_OPERATIONS_TYPE, "address"],
        bytes.fromhex(input_data[10:]),
        "EntryPoint handleOps",
    )
    if len(user_operations) != 1:
        raise ValueError("wrapped LP transaction must contain exactly one user operation")
    user_operation = user_operations[0]
    effective_sender = Web3.to_checksum_address(str(user_operation[0]))
    account_call = bytes(user_operation[3])
    if len(account_call) < 4 or _selector_hex(account_call) != ACCOUNT_EXECUTE_SELECTOR:
        raise ValueError("user operation has an unsupported account call")
    mode, execution_data = _decode_canonical_abi(
        ["bytes32", "bytes"],
        account_call[4:],
        "account execute",
    )
    if bytes(mode) != _BATCH_EXECUTION_MODE:
        raise ValueError("account execute mode is unsupported")
    batch_calls, = _decode_canonical_abi(
        ["(address,uint256,bytes)[]"],
        bytes(execution_data),
        "account batch",
    )
    matching_calls = [
        (index, bytes(call_data))
        for index, (target, _value, call_data) in enumerate(batch_calls)
        if Web3.to_checksum_address(str(target)) == position_manager_address
    ]
    if len(matching_calls) != 1:
        raise ValueError(
            "wrapped LP transaction must contain exactly one PositionManager call"
        )
    batch_index, position_manager_input = matching_calls[0]
    _validate_position_manager_multicall(position_manager_input)
    return (
        PositionManagerCallContext(
            input_data="0x" + position_manager_input.hex(),
            effective_sender=effective_sender,
            wrapper_source="entrypoint_handle_ops",
            user_operation_index=0,
            batch_index=batch_index,
            outer_transaction_hash=transaction_hash,
        ),
    )


def decode_liquidity_actions_for_tx(
    tx: dict[str, Any],
    receipt: dict[str, Any],
    block_timestamp: int,
    config: ExportPoolConfig,
    token_state: dict[int, LedgerPositionState],
    position_keys: dict[int, PoolPositionKey],
    *,
    wrapper_entrypoint: str,
    position_resolver: Callable[[int, int], LedgerPositionState | None] | None = None,
) -> list[DecodedLiquidityAction]:
    call_contexts = position_manager_calls_for_transaction(
        tx,
        config.position_manager,
        wrapper_entrypoint,
    )
    witnesses = _target_pool_modify_witnesses(receipt, config)
    if not call_contexts:
        if witnesses:
            raise ValueError("target-pool ModifyLiquidity witness has no PositionManager call")
        return []
    if len(call_contexts) != 1:
        raise ValueError("LP transaction has multiple PositionManager call contexts")
    context = call_contexts[0]
    actions, params = _decode_modify_liquidities_payload(context.input_data)
    if not actions:
        if witnesses:
            raise ValueError("target-pool ModifyLiquidity witness has no decoded action")
        return []

    working_state = dict(token_state)
    working_keys = dict(position_keys)
    decoded_actions: list[DecodedLiquidityAction] = []
    block_number = _int_from_rpc(tx["blockNumber"])
    timestamp_ms = block_timestamp * 1000
    block_time = datetime.fromtimestamp(block_timestamp, tz=timezone.utc).isoformat()
    tx_hash = context.outer_transaction_hash
    tx_targets_pool = False
    pending_take_index: int | None = None
    effective_sender = context.effective_sender
    add_action_indices: list[int] = []
    add_action_allowed_senders: dict[int, set[str]] = {}
    add_action_settled_currencies: dict[int, set[str]] = {}
    last_add_action_index: int | None = None
    mint_transfers = iter(
        _paired_mint_transfers(actions, receipt, config.position_manager)
    )
    _validate_take_pair_attribution(
        actions,
        params,
        effective_sender,
        config,
    )

    for event_order, (action, raw) in enumerate(zip(actions, params, strict=True)):
        action_code = _action_code(action)
        settlement_candidate_index = (
            last_add_action_index
            if action_code in {_V4_LP_SETTLE_PAIR, _V4_LP_SETTLE}
            else None
        )
        if action_code not in {_V4_LP_SETTLE_PAIR, _V4_LP_SETTLE}:
            last_add_action_index = None

        if action_code == _V4_LP_MINT_POSITION:
            mint_transfer = next(mint_transfers)
            token_id = mint_transfer.token_id
            (
                pool_key,
                tick_lower,
                tick_upper,
                liquidity_delta,
                _amount0_max,
                _amount1_max,
                recipient,
            ) = _decode_mint_param(raw)
            actual_recipient = _resolve_action_recipient(
                recipient,
                effective_sender,
                config.position_manager,
            )
            if mint_transfer.recipient != actual_recipient:
                raise ValueError(
                    "MINT_POSITION recipient does not match its creation Transfer"
                )
            if not _pool_key_matches(pool_key, config):
                continue
            if token_id in working_state or token_id in working_keys:
                raise ValueError(f"minted token id is already known: {token_id}")
            position_key = PoolPositionKey(
                pool_id=config.pool_id.lower(),
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                salt=_position_salt(token_id),
            )
            working_keys[token_id] = position_key
            working_state[token_id] = LedgerPositionState(
                pool_id=config.pool_id.lower(),
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                liquidity_after=liquidity_delta,
            )
            decoded_actions.append(
                _decoded_action(
                    action_type="mint",
                    block_number=block_number,
                    log_index=-1,
                    event_order=event_order,
                    token_id=token_id,
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    liquidity_delta=liquidity_delta,
                    amount0_raw=0,
                    amount1_raw=0,
                    collect_amount0=Decimal("0"),
                    collect_amount1=Decimal("0"),
                    block_time=block_time,
                    timestamp_ms=timestamp_ms,
                    tx_hash=tx_hash,
                    config=config,
                    amount0_attribution_source=OPENING_ATTRIBUTION_PENDING,
                    amount1_attribution_source=OPENING_ATTRIBUTION_PENDING,
                    amount_attribution_status=OPENING_ATTRIBUTION_PENDING,
                )
            )
            add_index = len(decoded_actions) - 1
            add_action_indices.append(add_index)
            add_action_allowed_senders[add_index] = {
                effective_sender,
                actual_recipient,
            }
            tx_targets_pool = True
            last_add_action_index = add_index
            continue

        if action_code in {
            _V4_LP_INCREASE_LIQUIDITY,
            _V4_LP_DECREASE_LIQUIDITY,
        }:
            (
                token_id,
                liquidity_delta,
                _amount0_max,
                _amount1_max,
            ) = _decode_increase_or_decrease_param(raw)
            position = _resolve_position_state(
                token_id,
                block_number,
                config,
                working_state,
                working_keys,
                position_resolver,
            )
            if position is None or position.pool_id.lower() != config.pool_id.lower():
                continue
            position_key = _required_matching_position_key(
                token_id,
                position,
                working_keys,
            )
            signed_delta = (
                liquidity_delta
                if action_code == _V4_LP_INCREASE_LIQUIDITY
                else -liquidity_delta
            )
            liquidity_after = position.liquidity_after + signed_delta
            if liquidity_after < 0:
                raise ValueError(f"liquidity_after below zero for token_id={token_id}")
            if signed_delta > 0:
                event_type = "mint"
                amount_source = OPENING_ATTRIBUTION_PENDING
                amount_status = OPENING_ATTRIBUTION_PENDING
            elif signed_delta < 0:
                event_type = "burn"
                amount_source = OPENING_ATTRIBUTION_SOURCE_NOT_APPLICABLE
                amount_status = OPENING_ATTRIBUTION_NOT_APPLICABLE
            else:
                event_type = "collect"
                amount_source = OPENING_ATTRIBUTION_SOURCE_NOT_APPLICABLE
                amount_status = OPENING_ATTRIBUTION_NOT_APPLICABLE
            decoded_actions.append(
                _decoded_action(
                    action_type=event_type,
                    block_number=block_number,
                    log_index=-1,
                    event_order=event_order,
                    token_id=token_id,
                    tick_lower=position_key.tick_lower,
                    tick_upper=position_key.tick_upper,
                    liquidity_delta=signed_delta,
                    amount0_raw=0,
                    amount1_raw=0,
                    collect_amount0=Decimal("0"),
                    collect_amount1=Decimal("0"),
                    block_time=block_time,
                    timestamp_ms=timestamp_ms,
                    tx_hash=tx_hash,
                    config=config,
                    amount0_attribution_source=amount_source,
                    amount1_attribution_source=amount_source,
                    amount_attribution_status=amount_status,
                )
            )
            working_state[token_id] = replace(
                position,
                liquidity_after=liquidity_after,
            )
            tx_targets_pool = True
            if signed_delta > 0:
                add_index = len(decoded_actions) - 1
                add_action_indices.append(add_index)
                add_action_allowed_senders[add_index] = {effective_sender}
                last_add_action_index = add_index
            else:
                if pending_take_index is not None:
                    raise ValueError(
                        "multiple unresolved target-pool liquidity withdrawals are "
                        "ambiguous"
                    )
                pending_take_index = len(decoded_actions) - 1
            continue

        if action_code == _V4_LP_BURN_POSITION:
            token_id = _decode_burn_param(raw)
            position = _resolve_position_state(
                token_id,
                block_number,
                config,
                working_state,
                working_keys,
                position_resolver,
            )
            if position is None or position.pool_id.lower() != config.pool_id.lower():
                continue
            position_key = _required_matching_position_key(
                token_id,
                position,
                working_keys,
            )
            if position.liquidity_after > 0:
                if pending_take_index is not None:
                    raise ValueError(
                        "multiple unresolved target-pool liquidity withdrawals are "
                        "ambiguous"
                    )
                decoded_actions.append(
                    _decoded_action(
                        action_type="burn",
                        block_number=block_number,
                        log_index=-1,
                        event_order=event_order,
                        token_id=token_id,
                        tick_lower=position_key.tick_lower,
                        tick_upper=position_key.tick_upper,
                        liquidity_delta=-position.liquidity_after,
                        amount0_raw=0,
                        amount1_raw=0,
                        collect_amount0=Decimal("0"),
                        collect_amount1=Decimal("0"),
                        block_time=block_time,
                        timestamp_ms=timestamp_ms,
                        tx_hash=tx_hash,
                        config=config,
                        amount0_attribution_source=(
                            OPENING_ATTRIBUTION_SOURCE_NOT_APPLICABLE
                        ),
                        amount1_attribution_source=(
                            OPENING_ATTRIBUTION_SOURCE_NOT_APPLICABLE
                        ),
                        amount_attribution_status=OPENING_ATTRIBUTION_NOT_APPLICABLE,
                    )
                )
                pending_take_index = len(decoded_actions) - 1
            working_state.pop(token_id)
            tx_targets_pool = True
            last_add_action_index = None
            continue

        if action_code == _V4_LP_SETTLE_PAIR:
            if settlement_candidate_index is None:
                continue
            currency0, currency1 = _decode_settle_pair_param(raw)
            if _settle_pair_matches(currency0, currency1, config):
                add_action_settled_currencies.setdefault(
                    settlement_candidate_index,
                    set(),
                ).update(_required_settlement_currencies(config))
            last_add_action_index = None
            continue

        if action_code == _V4_LP_SETTLE:
            if settlement_candidate_index is None:
                continue
            currency = _decode_settle_param(raw)
            if _settle_currency_matches(currency, config):
                add_action_settled_currencies.setdefault(
                    settlement_candidate_index,
                    set(),
                ).add(Web3.to_checksum_address(currency))
            last_add_action_index = settlement_candidate_index
            continue

        if action_code == _V4_LP_TAKE_PAIR:
            if not tx_targets_pool or pending_take_index is None:
                continue
            currency0, currency1, recipient = _decode_take_pair_param(raw)
            if not _settle_pair_matches(currency0, currency1, config):
                continue
            transfer_recipient = _resolve_action_recipient(
                recipient,
                effective_sender,
                config.position_manager,
            )
            collect_amount0, collect_amount1 = _extract_take_pair_amounts(
                receipt,
                transfer_recipient,
                config,
            )
            prior_action = decoded_actions[pending_take_index]
            decoded_actions[pending_take_index] = replace(
                prior_action,
                action_type=(
                    "burn_collect"
                    if prior_action.liquidity_delta < 0
                    else "collect"
                ),
                collect_amount0=collect_amount0,
                collect_amount1=collect_amount1,
            )
            pending_take_index = None
            last_add_action_index = None

    if pending_take_index is not None:
        raise ValueError(
            "target-pool liquidity withdrawal has no supported TAKE_PAIR attribution"
        )

    reconciled_actions = _reconcile_modify_liquidity_witnesses(
        decoded_actions,
        working_keys,
        witnesses,
        config,
    )
    attributed_actions = _apply_opening_amount_attribution(
        reconciled_actions,
        add_action_indices,
        add_action_allowed_senders,
        add_action_settled_currencies,
        receipt,
        config,
    )
    token_state.clear()
    token_state.update(working_state)
    position_keys.clear()
    position_keys.update(working_keys)
    return attributed_actions


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
        if (
            action.amount0_actual is None
            or action.amount1_actual is None
            or not action.amount0_attribution_source
            or not action.amount1_attribution_source
            or not action.amount_attribution_status
            or action.amount_attribution_status == OPENING_ATTRIBUTION_PENDING
        ):
            raise ValueError(
                "missing finalized amount attribution for "
                f"{action.action_type} token_id={action.token_id} at "
                f"{action.block_number}:{action.log_index}:{action.event_order}"
            )
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
                amount0_actual=_decimal(action.amount0_actual),
                amount1_actual=_decimal(action.amount1_actual),
                amount0_attribution_source=action.amount0_attribution_source,
                amount1_attribution_source=action.amount1_attribution_source,
                amount_attribution_status=action.amount_attribution_status,
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
    amount0_attribution_source: str,
    amount1_attribution_source: str,
    amount_attribution_status: str,
) -> DecodedLiquidityAction:
    amount0 = _token_amount(amount0_raw, config.token0_decimals)
    amount1 = _token_amount(amount1_raw, config.token1_decimals)
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
        amount0=amount0,
        amount1=amount1,
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
        amount0_actual=amount0,
        amount1_actual=amount1,
        amount0_attribution_source=amount0_attribution_source,
        amount1_attribution_source=amount1_attribution_source,
        amount_attribution_status=amount_attribution_status,
    )


def _apply_opening_amount_attribution(
    decoded_actions: list[DecodedLiquidityAction],
    add_action_indices: Sequence[int],
    add_action_allowed_senders: dict[int, set[str]],
    add_action_settled_currencies: dict[int, set[str]],
    receipt: dict[str, Any],
    config: ExportPoolConfig,
) -> list[DecodedLiquidityAction]:
    if not add_action_indices:
        return decoded_actions

    if len(add_action_indices) > 1:
        return [
            _replace_opening_amounts(
                action,
                amount0_raw=0,
                amount1_raw=0,
                config=config,
                amount0_attribution_source=OPENING_ATTRIBUTION_SOURCE_AMBIGUOUS,
                amount1_attribution_source=OPENING_ATTRIBUTION_SOURCE_AMBIGUOUS,
                amount_attribution_status=OPENING_ATTRIBUTION_AMBIGUOUS_MULTIPLE,
            )
            if index in add_action_indices
            else action
            for index, action in enumerate(decoded_actions)
        ]

    add_index = add_action_indices[0]
    if not _required_settlement_currencies(config).issubset(add_action_settled_currencies.get(add_index, set())):
        return _replace_action_at_index(
            decoded_actions,
            add_index,
            _replace_opening_amounts(
                decoded_actions[add_index],
                amount0_raw=0,
                amount1_raw=0,
                config=config,
                amount0_attribution_source=OPENING_ATTRIBUTION_SOURCE_AMBIGUOUS,
                amount1_attribution_source=OPENING_ATTRIBUTION_SOURCE_AMBIGUOUS,
                amount_attribution_status=OPENING_ATTRIBUTION_AMBIGUOUS_MISSING_SETTLE,
            ),
        )

    amount0_raw, amount1_raw = _extract_settlement_deposit_raw_amounts(
        receipt,
        config,
        add_action_allowed_senders[add_index],
    )
    if amount0_raw == 0 and amount1_raw == 0:
        return _replace_action_at_index(
            decoded_actions,
            add_index,
            _replace_opening_amounts(
                decoded_actions[add_index],
                amount0_raw=0,
                amount1_raw=0,
                config=config,
                amount0_attribution_source=OPENING_ATTRIBUTION_SOURCE_AMBIGUOUS,
                amount1_attribution_source=OPENING_ATTRIBUTION_SOURCE_AMBIGUOUS,
                amount_attribution_status=OPENING_ATTRIBUTION_AMBIGUOUS_NO_TRANSFER,
            ),
        )

    return _replace_action_at_index(
        decoded_actions,
        add_index,
        _replace_opening_amounts(
            decoded_actions[add_index],
            amount0_raw=amount0_raw,
            amount1_raw=amount1_raw,
            config=config,
            amount0_attribution_source=OPENING_ATTRIBUTION_SOURCE_TRANSFER,
            amount1_attribution_source=OPENING_ATTRIBUTION_SOURCE_TRANSFER,
            amount_attribution_status=OPENING_ATTRIBUTION_EXACT,
        ),
    )


def _replace_action_at_index(
    actions: list[DecodedLiquidityAction],
    index: int,
    replacement: DecodedLiquidityAction,
) -> list[DecodedLiquidityAction]:
    updated = list(actions)
    updated[index] = replacement
    return updated


def _replace_opening_amounts(
    action: DecodedLiquidityAction,
    *,
    amount0_raw: int,
    amount1_raw: int,
    config: ExportPoolConfig,
    amount0_attribution_source: str,
    amount1_attribution_source: str,
    amount_attribution_status: str,
) -> DecodedLiquidityAction:
    amount0 = _token_amount(amount0_raw, config.token0_decimals)
    amount1 = _token_amount(amount1_raw, config.token1_decimals)
    return replace(
        action,
        amount0=amount0,
        amount1=amount1,
        amount0_actual=amount0,
        amount1_actual=amount1,
        amount0_raw=str(amount0_raw),
        amount1_raw=str(amount1_raw),
        amount0_attribution_source=amount0_attribution_source,
        amount1_attribution_source=amount1_attribution_source,
        amount_attribution_status=amount_attribution_status,
    )


def _extract_settlement_deposit_raw_amounts(
    receipt: dict[str, Any],
    config: ExportPoolConfig,
    allowed_senders: set[str],
) -> tuple[int, int]:
    amount0_raw = 0
    amount1_raw = 0
    token0 = Web3.to_checksum_address(config.token0_address)
    token1 = Web3.to_checksum_address(config.token1_address)
    for log in receipt.get("logs", []):
        if not _is_pool_token_transfer_to_settlement(log, config, allowed_senders):
            continue
        token_addr = Web3.to_checksum_address(log["address"])
        amount_raw = _int_from_rpc(log["data"])
        if token_addr == token0:
            amount0_raw += amount_raw
        elif token_addr == token1:
            amount1_raw += amount_raw
    return amount0_raw, amount1_raw


def _is_pool_token_transfer_to_settlement(
    log: dict[str, Any],
    config: ExportPoolConfig,
    allowed_senders: set[str],
) -> bool:
    token_addresses = {
        Web3.to_checksum_address(config.token0_address),
        Web3.to_checksum_address(config.token1_address),
    }
    if Web3.to_checksum_address(log["address"]) not in token_addresses:
        return False
    topics = log.get("topics", [])
    if len(topics) < 3 or coerce_hex_str(topics[0]).lower() != TRANSFER_EVENT_TOPIC:
        return False
    sender = _address_from_topic(topics[1])
    if not allowed_senders or sender not in allowed_senders:
        return False
    recipient = _address_from_topic(topics[2])
    return recipient in _settlement_sink_addresses(config)


def _settlement_sink_addresses(config: ExportPoolConfig) -> set[str]:
    return {
        Web3.to_checksum_address(config.pool_manager),
        Web3.to_checksum_address(config.position_manager),
        Web3.to_checksum_address(settings.permit2_address),
    }


def _decode_settle_pair_param(raw: bytes) -> tuple[str, str]:
    currency0, currency1 = _decode_canonical_abi(
        ["address", "address"],
        raw,
        "SETTLE_PAIR parameter",
    )
    return Web3.to_checksum_address(str(currency0)), Web3.to_checksum_address(str(currency1))


def _decode_settle_param(raw: bytes) -> str:
    currency, = _decode_canonical_abi(["address"], raw, "SETTLE parameter")
    return Web3.to_checksum_address(str(currency))


def _settle_pair_matches(currency0: str, currency1: str, config: ExportPoolConfig) -> bool:
    return {
        Web3.to_checksum_address(currency0),
        Web3.to_checksum_address(currency1),
    } == _required_settlement_currencies(config)


def _settle_currency_matches(currency: str, config: ExportPoolConfig) -> bool:
    return Web3.to_checksum_address(currency) in _required_settlement_currencies(config)


def _required_settlement_currencies(config: ExportPoolConfig) -> set[str]:
    return {
        Web3.to_checksum_address(config.token0_address),
        Web3.to_checksum_address(config.token1_address),
    }


def _resolve_action_recipient(
    recipient: str,
    tx_sender: str,
    action_router: str,
) -> str:
    recipient = Web3.to_checksum_address(recipient)
    if recipient == ACTION_RECIPIENT_MSG_SENDER:
        return tx_sender
    if recipient == ACTION_RECIPIENT_ADDRESS_THIS:
        return Web3.to_checksum_address(action_router)
    return recipient


def _required_checksum_address(value: object, label: str) -> str:
    if not isinstance(value, str) or not Web3.is_address(value):
        raise ValueError(f"{label} is invalid")
    return Web3.to_checksum_address(value)


def _canonical_calldata(value: object, label: str) -> str:
    try:
        normalized = coerce_hex_str(value).lower()
        raw = bytes.fromhex(normalized.removeprefix("0x"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if len(raw) < 4 or normalized != "0x" + raw.hex():
        raise ValueError(f"{label} is invalid")
    return normalized


def _selector_hex(calldata: bytes) -> str:
    return "0x" + calldata[:4].hex()


def _decode_canonical_abi(
    types: Sequence[str],
    payload: bytes,
    label: str,
) -> tuple[Any, ...]:
    try:
        values = decode(types, payload, strict=True)
        canonical = encode(types, values)
    except (DecodingError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} ABI payload is invalid") from exc
    if canonical != payload:
        raise ValueError(f"{label} ABI payload is not canonical")
    return values


def _validate_position_manager_multicall(calldata: bytes) -> None:
    if len(calldata) < 4 or _selector_hex(calldata) != MULTICALL_SELECTOR:
        raise ValueError("wrapped PositionManager call is not multicall(bytes[])")
    calls, = _decode_canonical_abi(
        ["bytes[]"],
        calldata[4:],
        "PositionManager multicall",
    )
    if any(
        len(call) >= 4 and _selector_hex(bytes(call)) == MULTICALL_SELECTOR
        for call in calls
    ):
        raise ValueError("nested PositionManager multicall is unsupported")


def _decode_modify_liquidities_payload(input_data: str) -> tuple[bytes, list[bytes]]:
    input_hex = _canonical_calldata(input_data, "PositionManager input")
    selector = input_hex[:10].lower()
    raw = bytes.fromhex(input_hex[10:])
    if selector == MULTICALL_SELECTOR:
        calls, = _decode_canonical_abi(
            ["bytes[]"],
            raw,
            "PositionManager multicall",
        )
        actions = b""
        params: list[bytes] = []
        for call in calls:
            call_bytes = bytes(call)
            if len(call_bytes) < 4:
                continue
            call_selector = _selector_hex(call_bytes)
            if call_selector == MULTICALL_SELECTOR:
                raise ValueError("nested PositionManager multicall is unsupported")
            if call_selector != MODIFY_LIQUIDITIES_SELECTOR:
                continue
            call_actions, call_params = _decode_modify_liquidities_call(call_bytes[4:])
            actions += call_actions
            params.extend(call_params)
        return actions, params
    if selector != MODIFY_LIQUIDITIES_SELECTOR:
        return b"", []
    return _decode_modify_liquidities_call(raw)


def _decode_modify_liquidities_call(raw: bytes) -> tuple[bytes, list[bytes]]:
    unlock_data, _deadline = _decode_canonical_abi(
        ["bytes", "uint256"],
        raw,
        "modifyLiquidities",
    )
    actions, params = _decode_canonical_abi(
        ["bytes", "bytes[]"],
        bytes(unlock_data),
        "modifyLiquidities unlock data",
    )
    action_bytes = bytes(actions)
    parameter_bytes = [bytes(param) for param in params]
    if len(action_bytes) != len(parameter_bytes):
        raise ValueError("modifyLiquidities action and parameter counts differ")
    return action_bytes, parameter_bytes


def _decode_mint_param(raw: bytes) -> tuple[tuple[Any, ...], int, int, int, int, int, str]:
    (
        pool_key,
        tick_lower,
        tick_upper,
        liquidity,
        amount0_max,
        amount1_max,
        recipient,
        _hook,
    ) = _decode_canonical_abi(
        [
            "(address,address,uint24,int24,address)",
            "int24",
            "int24",
            "uint256",
            "uint128",
            "uint128",
            "address",
            "bytes",
        ],
        raw,
        "MINT_POSITION parameter",
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
    token_id, liquidity_delta, amount0, amount1, _hook = _decode_canonical_abi(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        raw,
        "liquidity-change parameter",
    )
    return int(token_id), int(liquidity_delta), int(amount0), int(amount1)


def _decode_burn_param(raw: bytes) -> int:
    token_id, _amount0, _amount1, _hook = _decode_canonical_abi(
        ["uint256", "uint128", "uint128", "bytes"],
        raw,
        "BURN_POSITION parameter",
    )
    return int(token_id)


def _decode_take_pair_param(raw: bytes) -> tuple[str, str, str]:
    currency0, currency1, recipient = _decode_canonical_abi(
        ["address", "address", "address"],
        raw,
        "TAKE_PAIR parameter",
    )
    return str(currency0), str(currency1), Web3.to_checksum_address(str(recipient))


def _pool_key_matches(pool_key: tuple[Any, ...], config: ExportPoolConfig) -> bool:
    if len(pool_key) != 5:
        raise ValueError("decoded PoolKey has the wrong field count")
    encoded = encode(
        ["(address,address,uint24,int24,address)"],
        [
            (
                Web3.to_checksum_address(str(pool_key[0])),
                Web3.to_checksum_address(str(pool_key[1])),
                int(pool_key[2]),
                int(pool_key[3]),
                Web3.to_checksum_address(str(pool_key[4])),
            )
        ],
    )
    pool_id = "0x" + Web3.keccak(encoded).hex().removeprefix("0x")
    return pool_id.lower() == config.pool_id.lower()


def _target_pool_modify_witnesses(
    receipt: dict[str, Any],
    config: ExportPoolConfig,
) -> tuple[_ModifyLiquidityWitness, ...]:
    witnesses: list[_ModifyLiquidityWitness] = []
    pool_manager = Web3.to_checksum_address(config.pool_manager)
    expected_pool_id = coerce_hex_str(config.pool_id).lower()
    for log in receipt.get("logs", []):
        if Web3.to_checksum_address(log["address"]) != pool_manager:
            continue
        topics = log.get("topics", [])
        if len(topics) < 2:
            continue
        if (
            coerce_hex_str(topics[0]).lower()
            != coerce_hex_str(V4_MODIFY_LIQUIDITY_TOPIC).lower()
        ):
            continue
        if coerce_hex_str(topics[1]).lower() != expected_pool_id:
            continue
        if len(topics) != 3:
            raise ValueError("target-pool ModifyLiquidity topics are malformed")
        data = _required_hex_bytes(log.get("data"), 128, "ModifyLiquidity data")
        tick_lower, tick_upper, liquidity_delta, salt = _decode_canonical_abi(
            ["int24", "int24", "int256", "bytes32"],
            data,
            "ModifyLiquidity log",
        )
        witnesses.append(
            _ModifyLiquidityWitness(
                log_index=_int_from_rpc(log["logIndex"]),
                pool_id=expected_pool_id,
                sender=_address_from_topic(topics[2]),
                tick_lower=int(tick_lower),
                tick_upper=int(tick_upper),
                liquidity_delta=int(liquidity_delta),
                salt="0x" + bytes(salt).hex(),
            )
        )
    ordered = tuple(sorted(witnesses, key=lambda witness: witness.log_index))
    if len({witness.log_index for witness in ordered}) != len(ordered):
        raise ValueError("target-pool ModifyLiquidity log index is duplicated")
    return ordered


def _paired_mint_transfers(
    actions: bytes,
    receipt: dict[str, Any],
    position_manager: str,
) -> tuple[_MintTransferWitness, ...]:
    mint_count = sum(
        action_code == _V4_LP_MINT_POSITION for action_code in actions
    )
    position_manager_address = Web3.to_checksum_address(position_manager)
    minted: list[_MintTransferWitness] = []
    for log in receipt.get("logs", []):
        if Web3.to_checksum_address(log["address"]) != position_manager_address:
            continue
        topics = log.get("topics", [])
        if not topics or coerce_hex_str(topics[0]).lower() != TRANSFER_EVENT_TOPIC:
            continue
        if len(topics) != 4:
            raise ValueError("PositionManager Transfer topics are malformed")
        _required_hex_bytes(log.get("data"), 0, "PositionManager Transfer data")
        if _none_if_zero_address(_address_from_topic(topics[1])) is not None:
            continue
        minted.append(
            _MintTransferWitness(
                log_index=_int_from_rpc(log["logIndex"]),
                token_id=int(
                    _required_word_hex(topics[3], "minted token id"),
                    16,
                ),
                recipient=_address_from_topic(topics[2]),
            )
        )
    minted.sort(key=lambda witness: witness.log_index)
    if len(minted) != mint_count:
        raise ValueError(
            "MINT_POSITION count does not match zero-address PositionManager Transfers"
        )
    if len({witness.log_index for witness in minted}) != len(minted):
        raise ValueError("mint creation Transfer log index is duplicated")
    if len({witness.token_id for witness in minted}) != len(minted):
        raise ValueError("minted token id is duplicated")
    return tuple(minted)


def _validate_take_pair_attribution(
    actions: bytes,
    params: Sequence[bytes],
    effective_sender: str,
    config: ExportPoolConfig,
) -> None:
    target_currencies = _required_settlement_currencies(config)
    take_pairs: list[tuple[set[str], str]] = []
    for action, raw in zip(actions, params, strict=True):
        if _action_code(action) != _V4_LP_TAKE_PAIR:
            continue
        currency0, currency1, recipient = _decode_take_pair_param(raw)
        take_pairs.append(
            (
                {
                    Web3.to_checksum_address(currency0),
                    Web3.to_checksum_address(currency1),
                },
                _resolve_action_recipient(
                    recipient,
                    effective_sender,
                    config.position_manager,
                ),
            )
        )

    target_indices = [
        index
        for index, (currencies, _recipient) in enumerate(take_pairs)
        if currencies == target_currencies
    ]
    if len(target_indices) > 1:
        raise ValueError("multiple target-pool TAKE_PAIR actions are ambiguous")
    if not target_indices:
        return

    target_index = target_indices[0]
    target_recipient = take_pairs[target_index][1]
    for index, (currencies, recipient) in enumerate(take_pairs):
        if index == target_index:
            continue
        if recipient == target_recipient and not currencies.isdisjoint(target_currencies):
            raise ValueError("TAKE_PAIR receipt attribution is ambiguous")


def _resolve_position_state(
    token_id: int,
    block_number: int,
    config: ExportPoolConfig,
    token_state: dict[int, LedgerPositionState],
    position_keys: dict[int, PoolPositionKey],
    position_resolver: Callable[[int, int], LedgerPositionState | None] | None,
) -> LedgerPositionState | None:
    position = token_state.get(token_id)
    if position is not None or position_resolver is None:
        return position
    position = position_resolver(token_id, max(block_number - 1, 0))
    if position is None:
        return None
    if position.liquidity_after < 0:
        raise ValueError(f"resolved liquidity is negative for token_id={token_id}")
    token_state[token_id] = position
    if position.pool_id.lower() == config.pool_id.lower():
        position_keys.setdefault(
            token_id,
            PoolPositionKey(
                pool_id=position.pool_id.lower(),
                tick_lower=position.tick_lower,
                tick_upper=position.tick_upper,
                salt=_position_salt(token_id),
            ),
        )
    return position


def _required_matching_position_key(
    token_id: int,
    position: LedgerPositionState,
    position_keys: dict[int, PoolPositionKey],
) -> PoolPositionKey:
    position_key = position_keys.get(token_id)
    if position_key is None:
        raise ValueError(f"missing PoolPositionKey for token_id={token_id}")
    if (
        position_key.pool_id.lower() != position.pool_id.lower()
        or position_key.tick_lower != position.tick_lower
        or position_key.tick_upper != position.tick_upper
        or position_key.salt.lower() != _position_salt(token_id)
    ):
        raise ValueError(f"PoolPositionKey does not match token state for token_id={token_id}")
    return position_key


def _reconcile_modify_liquidity_witnesses(
    actions: Sequence[DecodedLiquidityAction],
    position_keys: dict[int, PoolPositionKey],
    witnesses: Sequence[_ModifyLiquidityWitness],
    config: ExportPoolConfig,
) -> list[DecodedLiquidityAction]:
    remaining = list(witnesses)
    reconciled: list[DecodedLiquidityAction] = []
    expected_sender = Web3.to_checksum_address(config.position_manager)
    expected_pool_id = config.pool_id.lower()
    for action in actions:
        position_key = position_keys.get(action.token_id)
        if position_key is None:
            raise ValueError(
                "decoded action is missing a PoolPositionKey before ModifyLiquidity "
                "reconciliation"
            )
        if (
            action.pool_id.lower() != expected_pool_id
            or position_key.pool_id.lower() != expected_pool_id
            or action.tick_lower != position_key.tick_lower
            or action.tick_upper != position_key.tick_upper
        ):
            raise ValueError(
                "decoded action position does not match its PoolPositionKey"
            )
        match_index = next(
            (
                index
                for index, witness in enumerate(remaining)
                if witness.pool_id == expected_pool_id
                and witness.sender == expected_sender
                and witness.tick_lower == position_key.tick_lower
                and witness.tick_upper == position_key.tick_upper
                and witness.liquidity_delta == action.liquidity_delta
                and witness.salt == position_key.salt.lower()
            ),
            None,
        )
        if match_index is None:
            raise ValueError(
                "decoded action has no matching target-pool ModifyLiquidity witness"
            )
        witness = remaining.pop(match_index)
        reconciled.append(replace(action, log_index=witness.log_index))
    if remaining:
        raise ValueError(
            "target-pool ModifyLiquidity witness has no matching decoded action"
        )
    return reconciled


def _position_salt(token_id: int) -> str:
    if token_id < 0 or token_id >= 2**256:
        raise ValueError("token id is outside uint256")
    return "0x" + token_id.to_bytes(32, "big").hex()


def _required_hex_bytes(value: object, length: int, label: str) -> bytes:
    try:
        normalized = coerce_hex_str(value).lower()
        raw = bytes.fromhex(normalized.removeprefix("0x"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if len(raw) != length or normalized != "0x" + raw.hex():
        raise ValueError(f"{label} is invalid")
    return raw


def _required_word_hex(value: object, label: str) -> str:
    return "0x" + _required_hex_bytes(value, 32, label).hex()


def _extract_take_pair_amounts(
    receipt: dict[str, Any],
    recipient: str,
    config: ExportPoolConfig,
) -> tuple[Decimal, Decimal]:
    token0 = Web3.to_checksum_address(config.token0_address)
    token1 = Web3.to_checksum_address(config.token1_address)
    recipient = Web3.to_checksum_address(recipient)
    raw_amounts: dict[str, list[int]] = {token0: [], token1: []}
    for log in receipt.get("logs", []):
        if not _is_erc20_transfer_from_to(
            log,
            config.pool_manager,
            recipient,
        ):
            continue
        token_addr = Web3.to_checksum_address(log["address"])
        if token_addr in raw_amounts:
            raw_amounts[token_addr].append(_int_from_rpc(log["data"]))
    if any(len(amounts) > 1 for amounts in raw_amounts.values()):
        raise ValueError("TAKE_PAIR receipt attribution is ambiguous")
    amount0_raw = raw_amounts[token0][0] if raw_amounts[token0] else 0
    amount1_raw = raw_amounts[token1][0] if raw_amounts[token1] else 0
    return (
        _token_amount(amount0_raw, config.token0_decimals),
        _token_amount(amount1_raw, config.token1_decimals),
    )


def _is_erc20_transfer_from_to(
    log: dict[str, Any],
    sender: str,
    recipient: str,
) -> bool:
    topics = log.get("topics", [])
    if len(topics) != 3 or coerce_hex_str(topics[0]).lower() != TRANSFER_EVENT_TOPIC:
        return False
    return (
        _address_from_topic(topics[1]) == Web3.to_checksum_address(sender)
        and _address_from_topic(topics[2]) == Web3.to_checksum_address(recipient)
    )


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
