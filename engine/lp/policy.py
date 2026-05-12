"""Finite-state LP policy informed by reconstructed episode analytics."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from engine.lp.research import LPEpisode, LPResearchSummary, PriceRangeBucket


class LPPolicyAction(str, Enum):
    ENTER = "enter"
    HOLD = "hold"
    HARVEST = "harvest"
    RESET = "reset"
    DEFEND = "defend"


class LPPolicyState(str, Enum):
    IDLE = "idle"
    ACCUMULATE = "accumulate"
    ACTIVE = "active"
    HARVESTING = "harvesting"
    REPOSITIONING = "repositioning"
    DEFENSIVE = "defensive"


@dataclass(frozen=True, slots=True)
class LPPolicyConfig:
    profit_take_return: Decimal = Decimal("0.0025")
    stop_loss_return: Decimal = Decimal("-0.01")
    min_profit_traversal: Decimal = Decimal("0.08")
    max_boundary_overshoot: Decimal = Decimal("0.05")
    min_win_score: Decimal = Decimal("0.50")


@dataclass(frozen=True, slots=True)
class LPPolicyContext:
    has_position: bool
    open_episode: LPEpisode | None
    summary: LPResearchSummary | None
    current_price: Decimal | None
    range_min: Decimal | None
    range_max: Decimal | None
    strategy_fair_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LPPolicyDecision:
    state: LPPolicyState
    action: LPPolicyAction
    reason: str


def _boundary_overshoot_ratio(price: Decimal | None, a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if price is None or a is None or b is None or a <= 0 or b <= a:
        return None
    if price < a:
        return (a - price) / a
    if price > b:
        return (price - b) / b
    return Decimal("0")


def decide_lp_policy(context: LPPolicyContext, config: LPPolicyConfig | None = None) -> LPPolicyDecision:
    cfg = config or LPPolicyConfig()
    if not context.has_position:
        return LPPolicyDecision(LPPolicyState.IDLE, LPPolicyAction.ENTER, "no_active_position")

    episode = context.open_episode
    if episode is None:
        return LPPolicyDecision(LPPolicyState.ACTIVE, LPPolicyAction.HOLD, "active_position_without_episode_history")

    overshoot = _boundary_overshoot_ratio(context.current_price, context.range_min, context.range_max)
    if overshoot is not None and overshoot >= cfg.max_boundary_overshoot:
        return LPPolicyDecision(LPPolicyState.REPOSITIONING, LPPolicyAction.RESET, "price_outside_range")

    if episode.pnl_return is not None and episode.pnl_return <= cfg.stop_loss_return:
        return LPPolicyDecision(LPPolicyState.DEFENSIVE, LPPolicyAction.DEFEND, "stop_loss_breached")

    if (
        episode.pnl_return is not None
        and episode.delta_traversed is not None
        and episode.pnl_return >= cfg.profit_take_return
        and episode.delta_traversed >= cfg.min_profit_traversal
    ):
        return LPPolicyDecision(LPPolicyState.HARVESTING, LPPolicyAction.HARVEST, "profit_target_hit")

    if (
        context.summary is not None
        and context.summary.closed_episodes >= 3
        and context.summary.win_score < cfg.min_win_score
        and episode.end_bucket in {PriceRangeBucket.BELOW, PriceRangeBucket.ABOVE}
    ):
        return LPPolicyDecision(LPPolicyState.DEFENSIVE, LPPolicyAction.DEFEND, "weak_historical_win_score")

    return LPPolicyDecision(LPPolicyState.ACTIVE, LPPolicyAction.HOLD, "continue_holding")
