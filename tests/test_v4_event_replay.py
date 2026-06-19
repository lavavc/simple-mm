import pytest

from backtester.v4_event_replay import PoolStateSnapshot, ReplayEvent, attach_event_time_state


def test_liquidity_event_before_swap_gets_prior_price_not_swap_price():
    events = [
        ReplayEvent(100, 5, 0, "mint", None, None),
        ReplayEvent(100, 6, 0, "swap", 222, 2),
        ReplayEvent(100, 7, 0, "burn", None, None),
    ]

    replayed = attach_event_time_state(events, PoolStateSnapshot(111, 1, "prior_block"))

    assert replayed[0].event_time_sqrt_price_x96 == 111
    assert replayed[0].event_time_tick == 1
    assert replayed[0].event_time_state_source == "prior_block"
    assert replayed[1].event_time_sqrt_price_x96 == 222
    assert replayed[1].event_time_tick == 2
    assert replayed[1].event_time_state_source == "self_event"
    assert replayed[2].event_time_sqrt_price_x96 == 222
    assert replayed[2].event_time_tick == 2
    assert replayed[2].event_time_state_source == "same_block_prior_event"


def test_replay_sorts_by_block_log_index_and_event_order():
    events = [
        ReplayEvent(101, 1, 0, "burn", None, None),
        ReplayEvent(100, 7, 1, "mint", None, None),
        ReplayEvent(100, 6, 0, "swap", 222, 2),
        ReplayEvent(100, 7, 0, "collect", None, None),
    ]

    replayed = attach_event_time_state(events, PoolStateSnapshot(111, 1, "prior_block"))

    assert [(event.block_number, event.log_index, event.event_order) for event in replayed] == [
        (100, 6, 0),
        (100, 7, 0),
        (100, 7, 1),
        (101, 1, 0),
    ]
    assert [event.event_time_sqrt_price_x96 for event in replayed] == [222, 222, 222, 222]


def test_replay_marks_prior_event_source_across_blocks():
    events = [
        ReplayEvent(100, 6, 0, "swap", 222, 2),
        ReplayEvent(101, 1, 0, "mint", None, None),
    ]

    replayed = attach_event_time_state(events, PoolStateSnapshot(111, 1, "prior_block"))

    assert replayed[1].event_time_sqrt_price_x96 == 222
    assert replayed[1].event_time_state_source == "prior_event"


def test_initialize_can_seed_replay_without_prior_state():
    events = [
        ReplayEvent(100, 1, 0, "initialize", 333, 3),
        ReplayEvent(100, 2, 0, "mint", None, None),
    ]

    replayed = attach_event_time_state(events, None)

    assert replayed[0].event_time_sqrt_price_x96 == 333
    assert replayed[0].event_time_state_source == "self_event"
    assert replayed[1].event_time_sqrt_price_x96 == 333
    assert replayed[1].event_time_state_source == "same_block_prior_event"


def test_liquidity_without_seed_or_prior_price_fails():
    events = [ReplayEvent(100, 1, 0, "collect", None, None)]

    with pytest.raises(ValueError, match="no pool price state"):
        attach_event_time_state(events, None)


def test_swap_without_event_native_price_fails():
    events = [ReplayEvent(100, 1, 0, "swap", None, 1)]

    with pytest.raises(ValueError, match="missing event-native price"):
        attach_event_time_state(events, PoolStateSnapshot(111, 1, "prior_block"))
