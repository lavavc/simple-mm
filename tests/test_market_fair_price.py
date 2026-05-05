"""Tests for MarketFairPrice (Tier 1) and VarianceTracker."""

import time
from decimal import Decimal

import pytest

from engine.types import PriceQuote
from engine.market.price_aggregation import NormalizedPrice
from engine.market.venue_prices import VenuePrice
from engine.market.fair_price import (
    MarketFairPrice,
    MarketFairPriceCalculator,
    StrategyFairPrice,
    StrategyPriceCalculator,
    VarianceTracker,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_normalized(
    venue: str,
    cngn_usd: Decimal,
    volume_24h_usd: Decimal | None = None,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    timestamp_ms: int | None = None,
) -> NormalizedPrice:
    mid = cngn_usd
    b = bid if bid is not None else mid
    a = ask if ask is not None else mid
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return NormalizedPrice(
        venue=venue,
        cngn_usd=cngn_usd,
        raw_quote=PriceQuote(
            source=venue,
            timestamp=ts,
            bid=b,
            ask=a,
            mid=mid,
        ),
        basis="cNGN/USDC",
        timestamp=ts,
        volume_24h_usd=volume_24h_usd,
    )


def _make_venue_price(
    venue: str,
    pair: str = "cNGN/USDC",
    age_seconds: float = 0.0,
    volume_24h_usd: Decimal | None = None,
    cngn_usd: Decimal | None = None,
) -> VenuePrice:
    mid = cngn_usd if cngn_usd is not None else Decimal("0.000700")
    quote = PriceQuote(
        source=venue,
        timestamp=int(time.time() * 1000),
        bid=mid,
        ask=mid,
        mid=mid,
    )
    vp = VenuePrice(
        venue=venue,
        pair=pair,
        quote=quote,
        volume_24h_usd=volume_24h_usd,
        fetched_at=time.time() - age_seconds,
    )
    return vp


def _calc_no_pool() -> MarketFairPriceCalculator:
    """Calculator with empty pool_addresses dict (no DEX pool cache lookups)."""
    return MarketFairPriceCalculator(pool_addresses={})


# =============================================================================
# TestVarianceTracker
# =============================================================================


class TestVarianceTracker:
    def test_initial_variance_is_floor(self):
        """First update returns the floor — no prior price to compute a return."""
        tracker = VarianceTracker(floor_bps=3.0)
        result = tracker.update(0.000700)
        floor = (3.0 / 10_000) ** 2
        assert result == pytest.approx(floor)

    def test_variance_grows_on_moves(self):
        """Several moves drive σ² above the floor.

        Uses 0.05% (5 bps) moves — just under the initial 3σ jump filter
        threshold (floor_bps=3.0 → σ=3e-4, 3σ≈9e-4 ≈ 9 bps). Once EWMA
        is seeded above the floor, subsequent larger moves also pass.
        """
        tracker = VarianceTracker(lam=0.975, floor_bps=3.0)
        floor = (3.0 / 10_000) ** 2
        price = 0.000700
        # 5 bps moves pass the initial jump filter (threshold ~9 bps).
        for i in range(100):
            price *= 1.0005 if i % 2 == 0 else (1 / 1.0005)
            tracker.update(price)
        assert tracker.sigma_sq > floor

    def test_jump_filter_rejects_large_spike(self):
        """A 50% spike is treated as a data artifact; σ² stays unchanged."""
        tracker = VarianceTracker(lam=0.975, floor_bps=3.0)
        base = 0.000700

        # Seed a few small moves so _last_price is set and _var is near floor
        tracker.update(base)
        tracker.update(base * 1.001)
        tracker.update(base)

        var_before = tracker.sigma_sq

        # 50% spike — way beyond 3σ
        tracker.update(base * 1.50)

        # σ² must not have grown due to the spike
        assert tracker.sigma_sq == pytest.approx(var_before)

    def test_variance_is_direction_agnostic(self):
        """Up-then-down and down-then-up sequences of same magnitude give the same σ²."""
        lam = 0.975
        floor_bps = 3.0
        base = 0.000700
        step = 0.000007  # 1% of base

        tracker_up_down = VarianceTracker(lam=lam, floor_bps=floor_bps)
        tracker_up_down.update(base)
        tracker_up_down.update(base + step)
        tracker_up_down.update(base)

        tracker_down_up = VarianceTracker(lam=lam, floor_bps=floor_bps)
        tracker_down_up.update(base)
        tracker_down_up.update(base - step)
        tracker_down_up.update(base)

        assert tracker_up_down.sigma_sq == pytest.approx(tracker_down_up.sigma_sq)


# =============================================================================
# TestMarketFairPriceCalculator
# =============================================================================


class TestMarketFairPriceCalculator:
    def test_stale_venue_gets_low_weight(self):
        """A venue 700 s stale gets near-zero weight from recency decay."""
        calc = _calc_no_pool()

        stale_ts = int(time.time() * 1000) - 700_000  # 700 s ago in ms
        np_stale = _make_normalized("quidax", Decimal("0.000700"), timestamp_ms=stale_ts)
        np_fresh = _make_normalized("uni-base", Decimal("0.000700"))

        vp_stale = _make_venue_price("quidax", age_seconds=700.0)
        vp_fresh = _make_venue_price("uni-base", age_seconds=1.0)

        result = calc.compute(
            {"quidax": np_stale, "uni-base": np_fresh},
            {"quidax": vp_stale, "uni-base": vp_fresh},
        )

        stale_weight = result.weights.get("quidax", Decimal("0"))
        assert float(stale_weight) < 0.05

    def test_bybit_excluded(self):
        """bybit must not appear in MarketFairPrice.weights."""
        calc = _calc_no_pool()

        np_bybit = _make_normalized("bybit", Decimal("0.000697"))
        np_quidax = _make_normalized("quidax", Decimal("0.000700"))

        vp_bybit = _make_venue_price("bybit")
        vp_quidax = _make_venue_price("quidax")

        result = calc.compute(
            {"bybit": np_bybit, "quidax": np_quidax},
            {"bybit": vp_bybit, "quidax": vp_quidax},
        )

        assert "bybit" not in result.weights
        assert "quidax" in result.weights

    def test_single_venue_returns_its_price(self):
        """With one venue, the output price must equal that venue's price exactly."""
        calc = _calc_no_pool()

        price = Decimal("0.000697")
        np_single = _make_normalized("quidax", price, volume_24h_usd=Decimal("50000"))
        vp_single = _make_venue_price("quidax", age_seconds=0.0)

        result = calc.compute(
            {"quidax": np_single},
            {"quidax": vp_single},
        )

        assert float(result.price) == pytest.approx(float(price), rel=1e-6)
        assert float(result.weights["quidax"]) == pytest.approx(1.0, rel=1e-6)

    def test_higher_volume_venue_gets_higher_weight(self):
        """Two venues with identical recency and spread; 10× volume drives higher combined weight.

        Both _volume_weight and _liquidity_weight (CEX proxy path) read from np.volume_24h_usd,
        so a 10× volume difference produces a >10× combined factor difference.
        """
        calc = _calc_no_pool()

        low_vol = Decimal("10000")
        high_vol = Decimal("100000")

        np_low = _make_normalized("quidax", Decimal("0.000700"), volume_24h_usd=low_vol)
        np_high = _make_normalized("uni-base", Decimal("0.000700"), volume_24h_usd=high_vol)

        # Both fresh, identical age
        vp_low = _make_venue_price("quidax", age_seconds=1.0)
        vp_high = _make_venue_price("uni-base", age_seconds=1.0)

        result = calc.compute(
            {"quidax": np_low, "uni-base": np_high},
            {"quidax": vp_low, "uni-base": vp_high},
        )

        weight_low = float(result.weights["quidax"])
        weight_high = float(result.weights["uni-base"])
        assert weight_high > weight_low


# =============================================================================
# TestStrategyPriceCalculator
# =============================================================================


def _make_market_price(price: Decimal) -> MarketFairPrice:
    return MarketFairPrice(
        price=price,
        weights={"quidax": Decimal("1.0")},
        confidence=0.7,
        timestamp=int(time.time() * 1000),
    )


class TestStrategyPriceCalculator:
    def test_flat_inventory_zero_skew(self):
        """net_cngn == target_cngn → skew_bps == 0, strategy_price == market price."""
        calc = StrategyPriceCalculator(target_cngn=Decimal("500000"))
        mp = _make_market_price(Decimal("0.000700"))
        result = calc.compute(mp, net_cngn=Decimal("500000"))

        assert float(result.skew_bps) == pytest.approx(0.0, abs=1e-9)
        assert float(result.price) == pytest.approx(float(mp.price), rel=1e-9)
        assert result.executable_price == mp.price

    def test_long_cngn_negative_skew(self):
        """net_cngn > target_cngn → negative skew (long cNGN, want to sell → lower price)."""
        calc = StrategyPriceCalculator(target_cngn=Decimal("0"), beta_bps=10.0)
        mp = _make_market_price(Decimal("0.000700"))
        result = calc.compute(mp, net_cngn=Decimal("500000"))

        assert float(result.skew_bps) < 0.0
        assert float(result.price) < float(mp.price)

    def test_short_cngn_positive_skew(self):
        """net_cngn < target_cngn → positive skew (short cNGN, want to buy → higher price)."""
        calc = StrategyPriceCalculator(target_cngn=Decimal("1000000"), beta_bps=10.0)
        mp = _make_market_price(Decimal("0.000700"))
        result = calc.compute(mp, net_cngn=Decimal("500000"))

        assert float(result.skew_bps) > 0.0
        assert float(result.price) > float(mp.price)

    def test_extreme_inventory_capped_by_tanh(self):
        """net_cngn 100× larger than max_scale → |skew_bps| stays below max_skew_bps."""
        max_skew_bps = 20.0
        calc = StrategyPriceCalculator(
            target_cngn=Decimal("0"),
            max_scale=Decimal("1000000"),
            beta_bps=10.0,
            max_skew_bps=max_skew_bps,
        )
        mp = _make_market_price(Decimal("0.000700"))
        # 100× max_scale to drive tanh saturation
        result = calc.compute(mp, net_cngn=Decimal("100000000"))

        assert abs(float(result.skew_bps)) < max_skew_bps * 1.001

    def test_variance_tracker_updated(self):
        """After 20 compute() calls with varying prices, variance_tracker.sigma_sq exceeds floor."""
        floor_bps = 3.0
        floor = (floor_bps / 10_000) ** 2
        calc = StrategyPriceCalculator(variance_tracker=VarianceTracker(floor_bps=floor_bps))

        base_price = Decimal("0.000700")
        for i in range(20):
            # 5 bps alternating moves — small enough to pass jump filter
            factor = Decimal("1.0005") if i % 2 == 0 else Decimal("0.9995")
            base_price = base_price * factor
            mp = _make_market_price(base_price)
            calc.compute(mp, net_cngn=Decimal("0"))

        assert calc.variance_tracker.sigma_sq > floor
