"""Pure helpers for reconstructing V4 LP lifecycle ledger rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from backtester.clmm_math import cngn_price_from_sqrt_price_x96
from backtester.v4_event_replay import ReplayedEvent
from backtester.v4_export import POOL_CONFIGS, ExportPoolConfig


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


def _decimal(value: Decimal | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _action_sort_key(action: DecodedLiquidityAction) -> tuple[int, int, int]:
    return action.block_number, action.log_index, action.event_order


def _ownership_sort_key(event: OwnershipEvent) -> tuple[int, int, int]:
    return event.block_number, event.log_index, event.event_order
