"""Tests for arbitrage inventory tracking and circuit breakers."""

import pytest
from decimal import Decimal

from engine.api.schemas import ArbitrageParams
from engine.config import settings
from engine.core.arbitrage.inventory import InventoryTracker, InventoryState


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def params():
    return ArbitrageParams(
        max_daily_volume_usd=Decimal("5000"),
        max_daily_loss_usd=Decimal("100"),
        max_inventory_imbalance_usd=Decimal("2000"),
        max_consecutive_failures=3,
        max_single_trade_usd=Decimal("500"),
    )


@pytest.fixture
def tracker(params):
    return InventoryTracker(params)


# =============================================================================
# Initial state
# =============================================================================


class TestInitialState:

    def test_starts_at_zero(self, tracker):
        state = tracker.state
        assert tracker._rolling_volume_usd() == Decimal("0")
        assert state.daily_profit_usd == Decimal("0")
        assert state.daily_loss_usd == Decimal("0")
        assert state.cngn_imbalance_usd == Decimal("0")
        assert state.consecutive_failures == 0
        assert state.circuit_breaker_active is False

    def test_can_trade_initially(self, tracker):
        allowed, reason = tracker.can_trade(Decimal("100"))
        assert allowed is True
        assert reason is None


# =============================================================================
# Trade limits
# =============================================================================


class TestTradeLimits:

    def test_daily_volume_limit(self, tracker):
        """Should block trades that exceed daily volume."""
        # Fill up volume
        tracker.record_trade_complete("t1", Decimal("4500"), Decimal("10"), Decimal("0"))

        # Another $600 should push over the $5000 limit
        allowed, reason = tracker.can_trade(Decimal("600"))
        assert allowed is False
        assert "daily volume limit" in reason.lower()

    def test_under_volume_limit(self, tracker):
        tracker.record_trade_complete("t1", Decimal("2000"), Decimal("10"), Decimal("0"))
        allowed, reason = tracker.can_trade(Decimal("500"))
        assert allowed is True

    def test_inventory_imbalance_limit(self, tracker):
        """Should block trades when imbalance too high."""
        # Simulate large imbalance
        tracker._state.cngn_imbalance_usd = Decimal("1800")

        # Another $300 could push over $2000
        allowed, reason = tracker.can_trade(Decimal("300"))
        assert allowed is False
        assert "imbalance" in reason.lower()

    def test_daily_loss_limit(self, tracker):
        """Should block trades when daily loss limit reached."""
        # Record a big loss
        tracker.record_trade_complete("t1", Decimal("500"), Decimal("-100"), Decimal("0"))

        allowed, reason = tracker.can_trade(Decimal("100"))
        assert allowed is False
        assert "loss limit" in reason.lower()


# =============================================================================
# Circuit breaker
# =============================================================================


class TestCircuitBreaker:

    def test_triggers_on_consecutive_failures(self, tracker):
        """Circuit breaker should activate after max consecutive failures."""
        for i in range(3):
            tracker.record_trade_failure(f"opp-{i}", "timeout")

        assert tracker.state.circuit_breaker_active is True
        assert tracker.state.circuit_breaker_reason is not None

    def test_blocks_trading_when_active(self, tracker):
        """Active circuit breaker should block all trades."""
        for i in range(3):
            tracker.record_trade_failure(f"opp-{i}", "timeout")

        allowed, reason = tracker.can_trade(Decimal("100"))
        assert allowed is False
        assert "circuit breaker" in reason.lower()

    def test_manual_reset(self, tracker):
        """Manual reset should re-enable trading."""
        for i in range(3):
            tracker.record_trade_failure(f"opp-{i}", "timeout")

        assert tracker.state.circuit_breaker_active is True

        tracker.reset_circuit_breaker()
        assert tracker.state.circuit_breaker_active is False
        assert tracker.state.consecutive_failures == 0

        allowed, _ = tracker.can_trade(Decimal("100"))
        assert allowed is True

    def test_success_resets_failure_count(self, tracker):
        """A successful trade should reset consecutive failure count."""
        tracker.record_trade_failure("opp-1", "timeout")
        tracker.record_trade_failure("opp-2", "timeout")
        assert tracker.state.consecutive_failures == 2

        tracker.record_trade_complete("opp-3", Decimal("100"), Decimal("5"), Decimal("0"))
        assert tracker.state.consecutive_failures == 0

    def test_triggers_on_loss_limit(self, tracker):
        """Circuit breaker should trigger when daily loss limit exceeded."""
        tracker.record_trade_complete("t1", Decimal("500"), Decimal("-100"), Decimal("0"))
        assert tracker.state.circuit_breaker_active is True


# =============================================================================
# Recording trades
# =============================================================================


class TestTradeRecording:

    def test_record_profitable_trade(self, tracker):
        # Pass cngn_price_usd so imbalance = cngn_delta * price
        tracker.record_trade_complete(
            "t1", Decimal("500"), Decimal("25"), Decimal("100"),
            cngn_price_usd=Decimal("1"),  # 1 cNGN = $1 for easy math
        )

        assert tracker._rolling_volume_usd() == Decimal("500")
        assert tracker.state.daily_profit_usd == Decimal("25")
        assert tracker.state.daily_loss_usd == Decimal("0")
        assert tracker.state.cngn_imbalance_usd == Decimal("100")

    def test_record_losing_trade(self, tracker):
        # Pass cngn_price_usd so imbalance = cngn_delta * price
        tracker.record_trade_complete(
            "t1", Decimal("500"), Decimal("-10"), Decimal("-50"),
            cngn_price_usd=Decimal("1"),  # 1 cNGN = $1 for easy math
        )

        assert tracker._rolling_volume_usd() == Decimal("500")
        assert tracker.state.daily_profit_usd == Decimal("0")
        assert tracker.state.daily_loss_usd == Decimal("10")
        assert tracker.state.cngn_imbalance_usd == Decimal("-50")

    def test_multiple_trades_accumulate(self, tracker):
        tracker.record_trade_complete("t1", Decimal("200"), Decimal("10"), Decimal("50"))
        tracker.record_trade_complete("t2", Decimal("300"), Decimal("15"), Decimal("75"))

        assert tracker._rolling_volume_usd() == Decimal("500")
        assert tracker.state.daily_profit_usd == Decimal("25")

    def test_record_trade_start(self, tracker):
        tracker.record_trade_start("opp-1", Decimal("500"), "aerodrome", "quidax")
        assert tracker.state.last_trade_timestamp > 0


# =============================================================================
# Status dict
# =============================================================================


class TestStatusDict:

    def test_returns_expected_keys(self, tracker):
        status = tracker.get_status_dict()
        expected_keys = {
            "daily_volume_usd",
            "daily_profit_usd",
            "daily_loss_usd",
            "cngn_imbalance_usd",
            "consecutive_failures",
            "circuit_breaker_active",
            "circuit_breaker_reason",
            "low_inventory_venues",
        }
        assert set(status.keys()) == expected_keys

    def test_reflects_current_state(self, tracker):
        tracker.record_trade_complete("t1", Decimal("100"), Decimal("5"), Decimal("0"))
        status = tracker.get_status_dict()
        assert status["daily_volume_usd"] == Decimal("100")
        assert status["daily_profit_usd"] == Decimal("5")


# =============================================================================
# Per-account stablecoin tracking
# =============================================================================


class TestPerAccountStable:

    def test_initialize_seeds_correctly(self, tracker):
        tracker.initialize_account_stable({
            "aerodrome": Decimal("5000"),
            "pancakeswap": Decimal("3000"),
        })
        assert tracker._state.per_account_stable["aerodrome"] == Decimal("5000")
        assert tracker._state.initial_account_stable["aerodrome"] == Decimal("5000")

    def test_buy_reduces_balance(self, tracker):
        tracker.initialize_account_stable({"aerodrome": Decimal("5000")})
        tracker.update_account_inventory("aerodrome", Decimal("500"), is_buy=True)
        assert tracker._state.per_account_stable["aerodrome"] == Decimal("4500")

    def test_sell_increases_balance(self, tracker):
        tracker.initialize_account_stable({"pancakeswap": Decimal("2000")})
        tracker.update_account_inventory("pancakeswap", Decimal("300"), is_buy=False)
        assert tracker._state.per_account_stable["pancakeswap"] == Decimal("2300")

    def test_flags_low_when_below_threshold(self, tracker):
        threshold = Decimal(str(settings.arbitrage_min_account_stablecoin_usd))
        tracker.initialize_account_stable({"aerodrome": threshold * 2})
        # Drain to half the threshold
        tracker.update_account_inventory("aerodrome", threshold * Decimal("1.5"), is_buy=True)
        assert "aerodrome" in tracker._state.low_inventory_venues

    def test_clears_flag_when_above_threshold(self, tracker):
        threshold = Decimal(str(settings.arbitrage_min_account_stablecoin_usd))
        tracker.initialize_account_stable({"aerodrome": threshold * 2})
        tracker.update_account_inventory("aerodrome", threshold * Decimal("1.5"), is_buy=True)
        assert "aerodrome" in tracker._state.low_inventory_venues
        # Receive stablecoin — back above threshold
        tracker.update_account_inventory("aerodrome", threshold * Decimal("1.5"), is_buy=False)
        assert "aerodrome" not in tracker._state.low_inventory_venues


# =============================================================================
# get_rebalance_cost_bps
# =============================================================================


class TestGetRebalanceCost:

    def test_full_stock_returns_zero(self, tracker):
        tracker.initialize_account_stable({"aerodrome": Decimal("5000")})
        assert tracker.get_rebalance_cost_bps("aerodrome") == 0

    def test_half_drained_returns_half_cost(self, tracker):
        tracker.initialize_account_stable({"aerodrome": Decimal("5000")})
        tracker.update_account_inventory("aerodrome", Decimal("2500"), is_buy=True)
        cost = tracker.get_rebalance_cost_bps("aerodrome")
        assert cost == 5  # 50% drained → 5 of 10 bps

    def test_empty_returns_full_cost(self, tracker):
        tracker.initialize_account_stable({"aerodrome": Decimal("1000")})
        tracker.update_account_inventory("aerodrome", Decimal("1000"), is_buy=True)
        cost = tracker.get_rebalance_cost_bps("aerodrome")
        assert cost == 10

    def test_no_initial_data_returns_fallback(self, tracker):
        # No seeding — should return cross_chain_rebalance_bps
        cost = tracker.get_rebalance_cost_bps("aerodrome")
        assert cost == tracker.params.cross_chain_rebalance_bps


# =============================================================================
# Delta ratio check in can_trade
# =============================================================================


class TestDeltaRatioCheck:

    def test_no_snapshot_check_skipped(self, tracker):
        # total_portfolio_usd = 0 → check skipped
        allowed, reason = tracker.can_trade(Decimal("100"))
        assert allowed is True

    def test_under_max_delta_ratio_allowed(self, tracker):
        tracker.update_portfolio_snapshot(Decimal("500"), Decimal("1000"))  # 50% < 60%
        allowed, reason = tracker.can_trade(Decimal("100"))
        assert allowed is True

    def test_over_max_delta_ratio_blocked(self, tracker):
        tracker.update_portfolio_snapshot(Decimal("700"), Decimal("1000"))  # 70% > 60%
        allowed, reason = tracker.can_trade(Decimal("100"))
        assert allowed is False
        assert "max delta ratio" in reason.lower()

    def test_exactly_at_max_delta_ratio_blocked(self, tracker):
        tracker.update_portfolio_snapshot(Decimal("600"), Decimal("1000"))  # exactly 60%
        allowed, reason = tracker.can_trade(Decimal("100"))
        assert allowed is False


# =============================================================================
# can_trade with venue flags
# =============================================================================


class TestCanTradeWithVenueFlags:

    def test_buy_venue_flagged_is_blocked(self, tracker):
        tracker._state.low_inventory_venues.add("aerodrome")
        allowed, reason = tracker.can_trade(Decimal("100"), buy_venue="aerodrome")
        assert allowed is False
        assert "aerodrome" in reason.lower()

    def test_sell_venue_flagged_is_allowed(self, tracker):
        tracker._state.low_inventory_venues.add("pancakeswap")
        allowed, reason = tracker.can_trade(Decimal("100"), sell_venue="pancakeswap")
        assert allowed is True  # sell-side flag doesn't block

    def test_no_flag_is_allowed(self, tracker):
        allowed, reason = tracker.can_trade(Decimal("100"), buy_venue="aerodrome")
        assert allowed is True
