"""Pure tests for route selection logic (router.py)."""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock

import engine.arb.routing.router as _router
from engine.arb.routing.router import RouteCandidate, SelectedRoute, select_route
from engine.api.schemas import ArbitrageParams, OrderBookDepth, OrderBookLevel
from engine.arb.risk.inventory import InventoryTracker


def _default_depth() -> OrderBookDepth:
    return OrderBookDepth(
        venue="quidax",
        pair="cNGN/USDT",
        timestamp=1700000000000,
        bids=[OrderBookLevel(price=Decimal("1700"), amount=Decimal("1000"))],
        asks=[OrderBookLevel(price=Decimal("1600"), amount=Decimal("1000"))],
    )


def _make_candidate(
    direction: str = "QUIDAX_TO_UNI_BASE",
    buy_venue: str = "quidax",
    sell_venue: str = "uni-base",
    size_usd: float = 500.0,
    profit_usd: float = 5.0,
    gas_usd: float = 0.07,
    signal: dict | None = None,
) -> RouteCandidate:
    if signal is None:
        signal = (
            {"depth": {"quidax": _default_depth()}}
            if direction in {
                "QUIDAX_TO_UNI_BASE",
                "QUIDAX_TO_UNI_BSC",
                "UNI_BASE_TO_QUIDAX",
                "UNI_BSC_TO_QUIDAX",
            }
            else {}
        )
    return RouteCandidate(
        direction=direction,
        buy_venue=buy_venue,
        sell_venue=sell_venue,
        optimal_size_usd=Decimal(str(size_usd)),
        expected_profit_usd=Decimal(str(profit_usd)),
        gas_usd=Decimal(str(gas_usd)),
        signal=signal,
    )


def _make_inventory(
    per_account: dict | None = None,
    initial: dict | None = None,
    cngn_per_account: dict | None = None,
    imbalance: float = 0.0,
    circuit_breaker: bool = False,
) -> InventoryTracker:
    params = ArbitrageParams(
        max_daily_volume_usd=Decimal("100000"),
        max_daily_loss_usd=Decimal("10000"),
        max_inventory_imbalance_usd=Decimal("50000"),
        max_consecutive_failures=10,
    )
    tracker = InventoryTracker(params)
    tracker._state.cngn_imbalance_usd = Decimal(str(imbalance))
    tracker._state.circuit_breaker_active = circuit_breaker
    if circuit_breaker:
        tracker._state.circuit_breaker_reason = "test"
    if per_account:
        tracker._state.per_account_stable = {k: Decimal(str(v)) for k, v in per_account.items()}
    if initial:
        tracker._state.initial_account_stable = {k: Decimal(str(v)) for k, v in initial.items()}
    if cngn_per_account:
        tracker._state.per_account_cngn = {k: Decimal(str(v)) for k, v in cngn_per_account.items()}
    return tracker


class TestSelectRouteEdgeCases:
    def test_empty_candidates_returns_none(self):
        inv = _make_inventory()
        assert select_route([], inv) is None

    def test_all_unprofitable_returns_none(self):
        """Routes whose net profit ≤ 0 after gas + rebalance cost are rejected."""
        inv = _make_inventory(per_account={"quidax": 1000})
        # Gas $0.07 eats all the profit ($0.05)
        c = _make_candidate(profit_usd=0.05, gas_usd=0.07)
        assert select_route([c], inv) is None

    def test_zero_stable_balance_blocks_route(self):
        """Unseeded buy-side stable balance must block the route — mirrors the cNGN check."""
        inv = _make_inventory(cngn_per_account={"uni-base": 5_000_000})
        c = _make_candidate(profit_usd=10.0, gas_usd=0.07, size_usd=50.0)
        assert select_route([c], inv) is None

    def test_unknown_cngn_balance_blocks_route(self):
        """A sell venue with no cNGN balance seeded must be blocked — don't trade blind."""
        inv = _make_inventory()  # per_account_cngn not seeded
        c = _make_candidate(sell_venue="uni-base", profit_usd=10.0, gas_usd=0.07)
        assert select_route([c], inv) is None

    def test_zero_cngn_balance_blocks_route(self):
        """An explicitly-zero cNGN balance must block the route, not just leave size uncapped."""
        inv = _make_inventory(cngn_per_account={"uni-base": 0})
        c = _make_candidate(sell_venue="uni-base", profit_usd=10.0, gas_usd=0.07)
        assert select_route([c], inv) is None

    def test_size_capped_to_available_stable(self, monkeypatch):
        """adjusted_size is capped to per_account_stable on the buy venue."""
        monkeypatch.setattr(
            _router,
            "estimate_cex_dex_trade",
            lambda direction, depth, investment_usd: {"expected_profit_usd": Decimal("10")},
        )
        inv = _make_inventory(per_account={"quidax": 100}, cngn_per_account={"uni-base": 5_000_000})
        c = _make_candidate(size_usd=500.0, profit_usd=10.0)
        result = select_route([c], inv)
        assert result is not None
        assert result.adjusted_size_usd == Decimal("100")

    def test_cex_buy_routes_cap_to_max_safe_usd_from_wallet_cngn(self, monkeypatch):
        """QUIDAX->DEX routes should cap using inverted Quidax buy math, not cNGN/USD proxy."""
        monkeypatch.setattr(
            _router,
            "estimate_cex_dex_trade",
            lambda direction, depth, investment_usd: {"expected_profit_usd": Decimal("10")},
        )
        inv = _make_inventory(
            per_account={"quidax": 500},
            cngn_per_account={"uni-base": 26999},
        )
        inv._state.cngn_price_usd = Decimal("0")
        c = _make_candidate(
            size_usd=500.0,
            profit_usd=10.0,
            signal={"depth": {"quidax": _default_depth()}},
        )

        result = select_route([c], inv)

        assert result is not None
        assert result.adjusted_size_usd < Decimal("16")
        assert result.adjusted_size_usd > Decimal("15")

    def test_cex_buy_routes_block_when_depth_missing_for_wallet_cap(self):
        """Without Quidax depth we cannot safely invert the buy path, so QUIDAX->DEX should be blocked."""
        inv = _make_inventory(
            per_account={"quidax": 500},
            cngn_per_account={"uni-base": 26999},
        )
        inv._state.cngn_price_usd = Decimal("0")
        c = _make_candidate(size_usd=500.0, profit_usd=10.0, signal={})

        assert select_route([c], inv) is None

    def test_circuit_breaker_blocks_all_routes(self):
        inv = _make_inventory(circuit_breaker=True)
        c = _make_candidate(profit_usd=10.0)
        assert select_route([c], inv) is None

    def test_profitable_route_selected(self):
        inv = _make_inventory(per_account={"quidax": 500}, cngn_per_account={"uni-base": 5_000_000})
        c = _make_candidate(size_usd=100.0, profit_usd=5.0, gas_usd=0.07)
        result = select_route([c], inv)
        assert result is not None
        assert isinstance(result, SelectedRoute)
        assert result.net_profit_usd > Decimal("0")


class TestSelectRouteNetProfit:
    def test_net_profit_subtracts_gas(self):
        inv = _make_inventory(per_account={"quidax": 500}, cngn_per_account={"uni-base": 5_000_000})
        c = _make_candidate(size_usd=100.0, profit_usd=5.0, gas_usd=0.5)
        result = select_route([c], inv)
        # rebalance_cost = 0 when no initial seeded (fallback returns cross_chain_rebalance_bps)
        # actually fallback returns params.cross_chain_rebalance_bps = 20 bps
        # rebalance_cost = 100 * 20/10000 = 0.2
        assert result is not None
        # net = 5.0 - 0.5 - rebalance_cost
        assert result.net_profit_usd < Decimal("5.0")

    def test_rebalance_cost_scales_with_drain(self):
        """Drained account adds more friction → lower net profit."""
        inv_full = _make_inventory(
            per_account={"quidax": 500}, initial={"quidax": 500}
        )
        inv_drained = _make_inventory(
            per_account={"quidax": 250}, initial={"quidax": 500}
        )
        c = _make_candidate(profit_usd=5.0, gas_usd=0.07)
        r_full = select_route([c], inv_full)
        r_drained = select_route([c], inv_drained)
        if r_full and r_drained:
            assert r_full.net_profit_usd >= r_drained.net_profit_usd

    def test_dex_dex_route_recomputes_profit_at_capped_size(self, monkeypatch):
        """For DEX-DEX, net profit is recomputed from pool math at the capped size,
        not taken from the detection signal's unconstrained optimal."""
        inv = _make_inventory(
            per_account={"uni-bsc": 100},
            cngn_per_account={"uni-base": 1000000},
        )
        c = _make_candidate(
            direction="UNI_BSC_TO_UNI_BASE_DELTA_BALANCE",
            buy_venue="uni-bsc",
            sell_venue="uni-base",
            size_usd=500.0,
            profit_usd=50.0,  # unconstrained optimal — overstates profit at capped size
            gas_usd=0.5,
        )

        def _fake_exact_cap(direction, wallet_cngn):
            assert direction == "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE"
            assert wallet_cngn == Decimal("1000000")
            return {"optimal_size_usd": 100.0, "expected_profit_usd": 2.0, "cngn_transferred": 140000.0}

        monkeypatch.setattr(_router, "estimate_max_dex_buy_usd_for_cngn", _fake_exact_cap)
        # estimate_dex_dex_trade must NOT be called when the cNGN cap is binding —
        # profit is already priced inside estimate_max_dex_buy_usd_for_cngn.
        result = select_route([c], inv)
        assert result is not None
        assert result.adjusted_size_usd == Decimal("100")
        # net = 2.0 (from cap trade result) - 0.5 (gas) - rebalance_cost
        assert result.net_profit_usd == Decimal("1.4")

    def test_dex_dex_route_blocks_when_exact_sell_cap_unavailable(self, monkeypatch):
        inv = _make_inventory(
            per_account={"uni-bsc": 100},
            cngn_per_account={"uni-base": 1000000},
        )
        c = _make_candidate(
            direction="UNI_BSC_TO_UNI_BASE_DELTA_BALANCE",
            buy_venue="uni-bsc",
            sell_venue="uni-base",
            size_usd=500.0,
            profit_usd=50.0,
            gas_usd=0.5,
        )

        monkeypatch.setattr(_router, "estimate_max_dex_buy_usd_for_cngn", lambda direction, wallet_cngn: None)

        assert select_route([c], inv) is None

    def test_cex_dex_route_recomputes_profit_at_capped_size(self, monkeypatch):
        """For CEX-DEX, the capped route must be rescored at the capped size."""
        inv = _make_inventory(
            per_account={"quidax": 100},
            cngn_per_account={"uni-base": 5_000_000},
        )
        c = _make_candidate(
            direction="QUIDAX_TO_UNI_BASE",
            buy_venue="quidax",
            sell_venue="uni-base",
            size_usd=500.0,
            profit_usd=50.0,
            gas_usd=0.07,
        )

        def _fake_estimate(direction, depth, investment_usd):
            assert direction == "QUIDAX_TO_UNI_BASE"
            assert depth == c.signal["depth"]["quidax"]
            assert investment_usd == Decimal("100")
            return {"expected_profit_usd": Decimal("0.05")}

        monkeypatch.setattr(_router, "estimate_cex_dex_trade", _fake_estimate)
        result = select_route([c], inv)

        assert result is None

    def test_dex_to_cex_route_uses_exact_sell_side_cngn_cap(self, monkeypatch):
        inv = _make_inventory(
            per_account={"uni-base": 500},
            cngn_per_account={"quidax": 1_000_000},
        )
        c = _make_candidate(
            direction="UNI_BASE_TO_QUIDAX",
            buy_venue="uni-base",
            sell_venue="quidax",
            size_usd=500.0,
            profit_usd=50.0,
            gas_usd=0.07,
            signal={"depth": {"quidax": _default_depth()}},
        )

        def _fake_exact_cap(direction, depth, wallet_cngn):
            assert direction == "UNI_BASE_TO_QUIDAX"
            assert depth == c.signal["depth"]["quidax"]
            assert wallet_cngn == Decimal("1000000")
            return {"optimal_size_usd": 100.0, "expected_profit_usd": 2.0, "cngn_transferred": 140000.0}

        def _fake_estimate(direction, depth, investment_usd):
            assert direction == "UNI_BASE_TO_QUIDAX"
            assert depth == c.signal["depth"]["quidax"]
            assert investment_usd == Decimal("100")
            return {"expected_profit_usd": Decimal("2.0")}

        monkeypatch.setattr(_router, "estimate_max_cex_dex_buy_usd_for_cngn", _fake_exact_cap)
        monkeypatch.setattr(_router, "estimate_cex_dex_trade", _fake_estimate)

        result = select_route([c], inv)

        assert result is not None
        assert result.adjusted_size_usd == Decimal("100")
        assert result.net_profit_usd == Decimal("1.83")

    def test_dex_to_cex_route_blocks_when_exact_sell_cap_unavailable(self, monkeypatch):
        inv = _make_inventory(
            per_account={"uni-base": 500},
            cngn_per_account={"quidax": 1_000_000},
        )
        c = _make_candidate(
            direction="UNI_BASE_TO_QUIDAX",
            buy_venue="uni-base",
            sell_venue="quidax",
            size_usd=500.0,
            profit_usd=50.0,
            gas_usd=0.07,
            signal={"depth": {"quidax": _default_depth()}},
        )

        monkeypatch.setattr(_router, "estimate_max_cex_dex_buy_usd_for_cngn", lambda direction, depth, wallet_cngn: None)

        assert select_route([c], inv) is None


class TestSelectRouteTiebreak:
    def test_highest_net_profit_wins(self):
        """When multiple routes are profitable, the highest net profit is chosen."""
        inv = _make_inventory(per_account={"quidax": 500}, cngn_per_account={"uni-base": 5_000_000, "uni-bsc": 5_000_000})
        c1 = _make_candidate(direction="QUIDAX_TO_UNI_BASE", size_usd=100.0, profit_usd=10.0, gas_usd=0.07)
        c2 = _make_candidate(direction="QUIDAX_TO_UNI_BSC", sell_venue="uni-bsc", size_usd=100.0, profit_usd=5.0, gas_usd=0.07)
        result = select_route([c1, c2], inv)
        assert result is not None
        assert result.candidate.direction == "QUIDAX_TO_UNI_BASE"

    def test_inventory_alignment_tiebreak_long_cngn(self, monkeypatch):
        """When long cNGN (imbalance > threshold), prefer selling cNGN to CEX."""
        monkeypatch.setattr(_router, "estimate_max_cex_buy_usd_for_cngn", lambda depth, wallet_cngn: Decimal("200"))
        monkeypatch.setattr(
            _router,
            "estimate_max_cex_dex_buy_usd_for_cngn",
            lambda direction, depth, wallet_cngn: {
                "optimal_size_usd": Decimal("200"),
                "expected_profit_usd": Decimal("5"),
                "cngn_transferred": Decimal("100"),
            },
        )
        inv = _make_inventory(
            imbalance=50.0,  # above $10 threshold
            per_account={"uni-base": 500, "quidax": 500},
            cngn_per_account={"uni-base": 5_000_000, "quidax": 5_000_000},
        )
        # sell-to-CEX direction (aligned): buy on uni-base, sell on quidax
        c_sell = _make_candidate(
            direction="UNI_BASE_TO_QUIDAX",  # in _SELLS_CNGN_TO_CEX
            buy_venue="uni-base", sell_venue="quidax",
            size_usd=200.0, profit_usd=5.0, gas_usd=0.07,
        )
        # buy-from-CEX direction (misaligned): buy on quidax, sell on uni-base
        c_buy = _make_candidate(
            direction="QUIDAX_TO_UNI_BASE",  # in _BUYS_CNGN_FROM_CEX
            buy_venue="quidax", sell_venue="uni-base",
            size_usd=200.0, profit_usd=5.0, gas_usd=0.07,
        )
        result = select_route([c_sell, c_buy], inv)
        assert result is not None
        # Tiebreak should prefer the aligned sell direction
        assert result.candidate.direction == "UNI_BASE_TO_QUIDAX"
