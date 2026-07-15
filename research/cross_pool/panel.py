"""Causal regular-clock panels for asynchronous pool event streams."""

from __future__ import annotations

from math import log
from typing import Sequence

from research.cross_pool.contracts import (
    CausalPanel,
    CrossPoolContractError,
    PanelConfig,
    PanelRow,
    PoolEvent,
    PoolName,
    Regime,
)


def build_causal_panel(
    base: Sequence[PoolEvent],
    bsc: Sequence[PoolEvent],
    config: PanelConfig,
) -> CausalPanel:
    _validate_config(config)
    _validate_events(base, expected_pool="uni-base")
    _validate_events(bsc, expected_pool="uni-bsc")

    common_start = max(base[0].timestamp_ms, bsc[0].timestamp_ms)
    common_end = min(base[-1].timestamp_ms, bsc[-1].timestamp_ms)
    first_decision = _ceil_to_multiple(
        common_start + config.horizon_ms,
        config.horizon_ms,
    )
    last_decision = ((common_end - config.horizon_ms) // config.horizon_ms) * config.horizon_ms
    if first_decision > last_decision:
        raise CrossPoolContractError(
            "common interval has no complete causal panel row with t-h, t, and t+h"
        )

    endpoint_times = tuple(
        range(
            first_decision - config.horizon_ms,
            last_decision + config.horizon_ms + 1,
            config.horizon_ms,
        )
    )
    base_states = _as_of_states(base, endpoint_times)
    bsc_states = _as_of_states(bsc, endpoint_times)

    rows = tuple(
        _build_row(
            timestamp_ms=endpoint_times[index],
            horizon_ms=config.horizon_ms,
            base_lag=base_states[index - 1],
            base_state=base_states[index],
            base_forward=base_states[index + 1],
            bsc_lag=bsc_states[index - 1],
            bsc_state=bsc_states[index],
            bsc_forward=bsc_states[index + 1],
            base_transition_block=config.base_transition_block,
            bsc_transition_block=config.bsc_transition_block,
        )
        for index in range(1, len(endpoint_times) - 1)
    )
    return CausalPanel(
        common_interval_start_ms=common_start,
        common_interval_end_ms=common_end,
        horizon_ms=config.horizon_ms,
        rows=rows,
    )


def _validate_config(config: PanelConfig) -> None:
    if config.horizon_ms <= 0:
        raise CrossPoolContractError("horizon_ms must be positive")
    if config.base_transition_block <= 0 or config.bsc_transition_block <= 0:
        raise CrossPoolContractError("transition blocks must be positive")


def _validate_events(
    events: Sequence[PoolEvent],
    *,
    expected_pool: PoolName,
) -> None:
    if not events:
        raise CrossPoolContractError(f"panel requires at least one {expected_pool} event")
    if any(event.pool != expected_pool for event in events):
        raise CrossPoolContractError(f"{expected_pool} stream contains an unexpected pool")
    if any(
        current.timestamp_ms <= previous.timestamp_ms
        for previous, current in zip(events, events[1:], strict=False)
    ):
        raise CrossPoolContractError(f"{expected_pool} timestamps must be strictly increasing")


def _ceil_to_multiple(value: int, interval: int) -> int:
    return -(-value // interval) * interval


def _as_of_states(
    events: Sequence[PoolEvent],
    query_times: Sequence[int],
) -> tuple[PoolEvent, ...]:
    states: list[PoolEvent] = []
    event_index = 0
    state: PoolEvent | None = None
    for query_time in query_times:
        while event_index < len(events) and events[event_index].timestamp_ms <= query_time:
            state = events[event_index]
            event_index += 1
        if state is None:
            raise CrossPoolContractError(
                f"no pool state is observable at query timestamp {query_time}"
            )
        states.append(state)
    return tuple(states)


def _build_row(
    *,
    timestamp_ms: int,
    horizon_ms: int,
    base_lag: PoolEvent,
    base_state: PoolEvent,
    base_forward: PoolEvent,
    bsc_lag: PoolEvent,
    bsc_state: PoolEvent,
    bsc_forward: PoolEvent,
    base_transition_block: int,
    bsc_transition_block: int,
) -> PanelRow:
    return PanelRow(
        timestamp_ms=timestamp_ms,
        horizon_ms=horizon_ms,
        base_state_timestamp_ms=base_state.timestamp_ms,
        bsc_state_timestamp_ms=bsc_state.timestamp_ms,
        base_forward_state_timestamp_ms=base_forward.timestamp_ms,
        bsc_forward_state_timestamp_ms=bsc_forward.timestamp_ms,
        base_lag_block_number=base_lag.block_number,
        bsc_lag_block_number=bsc_lag.block_number,
        base_state_block_number=base_state.block_number,
        bsc_state_block_number=bsc_state.block_number,
        base_forward_block_number=base_forward.block_number,
        bsc_forward_block_number=bsc_forward.block_number,
        base_age_ms=timestamp_ms - base_state.timestamp_ms,
        bsc_age_ms=timestamp_ms - bsc_state.timestamp_ms,
        base_mid=float(base_state.raw_mid),
        bsc_mid=float(bsc_state.raw_mid),
        base_trailing_return_bps=_log_return_bps(base_lag, base_state),
        bsc_trailing_return_bps=_log_return_bps(bsc_lag, bsc_state),
        base_minus_bsc_gap_bps=_log_return_bps(bsc_state, base_state),
        base_forward_return_bps=_log_return_bps(base_state, base_forward),
        bsc_forward_return_bps=_log_return_bps(bsc_state, bsc_forward),
        base_regime=_regime(
            base_lag,
            base_state,
            base_forward,
            transition_block=base_transition_block,
        ),
        bsc_regime=_regime(
            bsc_lag,
            bsc_state,
            bsc_forward,
            transition_block=bsc_transition_block,
        ),
    )


def _log_return_bps(start: PoolEvent, end: PoolEvent) -> float:
    return 10_000.0 * log(float(end.raw_mid / start.raw_mid))


def _regime(
    lag: PoolEvent,
    state: PoolEvent,
    forward: PoolEvent,
    *,
    transition_block: int,
) -> Regime:
    blocks = (lag.block_number, state.block_number, forward.block_number)
    if all(block < transition_block for block in blocks):
        return "early"
    if all(block >= transition_block for block in blocks):
        return "late"
    return "mixed"
