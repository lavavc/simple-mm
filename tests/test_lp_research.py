from __future__ import annotations

from decimal import Decimal

from engine.lp.policy import LPPolicyAction, LPPolicyContext, decide_lp_policy
from engine.lp.research import PriceRangeBucket, analyze_lp_strategy


def _snapshot(
    timestamp: int,
    token_id: str,
    current_price: str,
    position_value: str,
    fraction: str,
    in_range: bool = True,
) -> dict:
    return {
        "venue": "uni-base",
        "pair": "cNGN/USDC",
        "timestamp": timestamp,
        "balances": {"cngn": 0.0, "usdc": 0.0},
        "position_value_usd": float(position_value),
        "lp_position": {
            "token_id": token_id,
            "tick_lower": -100,
            "tick_upper": 100,
            "range_min": 1.0,
            "range_max": 2.0,
            "current_price": float(current_price),
            "price_position_fraction": float(fraction),
            "in_range": in_range,
            "our_share_pct": 12.5,
        },
    }


def test_analyze_lp_strategy_reconstructs_closed_and_open_episodes():
    actions = [
        {
            "timestamp": 30,
            "venue": "uni-base",
            "action_type": "remove_position",
            "status": "confirmed",
            "metadata": {"token_id": 1},
        }
    ]
    snapshots = [
        _snapshot(10, "1", "1.20", "1000", "0.20"),
        _snapshot(20, "1", "1.35", "1030", "0.35"),
        _snapshot(40, "2", "1.48", "980", "0.48"),
        _snapshot(50, "2", "1.62", "1008", "0.62"),
    ]

    report = analyze_lp_strategy("uni-base", actions, snapshots)

    assert report.summary.total_episodes == 2
    assert report.summary.closed_episodes == 1
    assert report.summary.profitable_episodes == 1
    assert report.summary.profitable_share == Decimal("1")
    assert report.summary.dominant_position_type == "in_range_to_in_range"

    first = report.episodes[0]
    assert first.closed is True
    assert first.pnl_usd == Decimal("30")
    assert first.pnl_return == Decimal("0.03")
    assert first.start_bucket == PriceRangeBucket.IN_RANGE
    assert first.end_bucket == PriceRangeBucket.IN_RANGE
    assert first.delta_traversed == Decimal("0.15")

    assert report.open_episode is not None
    assert report.open_episode.token_id == "2"
    assert report.open_episode.closed is False


def test_policy_harvests_when_return_and_traversal_hit_targets():
    report = analyze_lp_strategy(
        "uni-base",
        [],
        [
            _snapshot(100, "9", "1.30", "1000", "0.45"),
            _snapshot(200, "9", "1.42", "1006", "0.60"),
        ],
    )
    decision = decide_lp_policy(
        LPPolicyContext(
            has_position=True,
            open_episode=report.open_episode,
            summary=report.summary,
            current_price=Decimal("1.42"),
            range_min=Decimal("1"),
            range_max=Decimal("2"),
        )
    )
    assert decision.action == LPPolicyAction.HARVEST
    assert decision.reason == "profit_target_hit"


def test_policy_resets_when_price_is_outside_range():
    report = analyze_lp_strategy(
        "uni-base",
        [],
        [
            _snapshot(100, "9", "1.30", "1000", "0.45"),
            _snapshot(200, "9", "2.20", "950", "1.20", in_range=False),
        ],
    )
    decision = decide_lp_policy(
        LPPolicyContext(
            has_position=True,
            open_episode=report.open_episode,
            summary=report.summary,
            current_price=Decimal("2.20"),
            range_min=Decimal("1"),
            range_max=Decimal("2"),
        )
    )
    assert decision.action == LPPolicyAction.RESET
    assert decision.reason == "price_outside_range"
