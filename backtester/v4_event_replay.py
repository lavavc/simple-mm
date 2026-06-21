"""Event-time V4 pool price replay helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PoolStateSnapshot:
    sqrt_price_x96: int
    tick: int
    source: str


@dataclass(frozen=True)
class ReplayEvent:
    block_number: int
    log_index: int
    event_order: int
    event_type: str
    sqrt_price_x96: int | None
    tick: int | None


@dataclass(frozen=True)
class ReplayedEvent:
    block_number: int
    log_index: int
    event_order: int
    event_type: str
    sqrt_price_x96: int | None
    tick: int | None
    event_time_sqrt_price_x96: int
    event_time_tick: int
    event_time_state_source: str


_PRICE_EVENTS = {"initialize", "swap"}
_CARRIED_STATE_EVENTS = {"mint", "burn", "collect", "burn_collect"}


def attach_event_time_state(
    events: Sequence[ReplayEvent],
    initial_state: PoolStateSnapshot | None,
) -> list[ReplayedEvent]:
    replayed: list[ReplayedEvent] = []
    current_state = initial_state
    current_state_block: int | None = None

    for event in sorted(events, key=lambda value: (value.block_number, value.log_index, value.event_order)):
        if event.event_type in _PRICE_EVENTS:
            if event.sqrt_price_x96 is None or event.tick is None:
                raise ValueError(f"{event.event_type} event missing event-native price")
            replayed.append(
                ReplayedEvent(
                    block_number=event.block_number,
                    log_index=event.log_index,
                    event_order=event.event_order,
                    event_type=event.event_type,
                    sqrt_price_x96=event.sqrt_price_x96,
                    tick=event.tick,
                    event_time_sqrt_price_x96=event.sqrt_price_x96,
                    event_time_tick=event.tick,
                    event_time_state_source="self_event",
                )
            )
            current_state = PoolStateSnapshot(
                sqrt_price_x96=event.sqrt_price_x96,
                tick=event.tick,
                source="prior_event",
            )
            current_state_block = event.block_number
            continue

        if event.event_type not in _CARRIED_STATE_EVENTS:
            raise ValueError(f"unsupported replay event type: {event.event_type}")
        if current_state is None:
            raise ValueError(f"{event.event_type} event has no pool price state")
        state_source = (
            "same_block_prior_event"
            if current_state_block == event.block_number
            else current_state.source
        )
        replayed.append(
            ReplayedEvent(
                block_number=event.block_number,
                log_index=event.log_index,
                event_order=event.event_order,
                event_type=event.event_type,
                sqrt_price_x96=event.sqrt_price_x96,
                tick=event.tick,
                event_time_sqrt_price_x96=current_state.sqrt_price_x96,
                event_time_tick=current_state.tick,
                event_time_state_source=state_source,
            )
        )

    return replayed
