"""Full LP lifecycle tests using FakeDexAdapter.

Each test walks a complete state transition in the LP lifecycle: from
wallet funds through minting, topup, rebalancing (out-of-range), and
withdrawal. These tests prove the orchestrator logic in LPRebalancer —
not the math (that's in test_price_math.py) or the ratio computation
(test_lp_ratio.py).

Every test asserts final position state AND the action log, to ensure
the rebalancer both executes and records each step correctly.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import pytest

from engine.db.connection import SQLiteConnectionManager
from engine.db.repository import DatabaseRepository
from engine.lp.rebalancer import LPRebalancer
from engine.types import PriceQuote
from engine.venues.dex.shared import PositionState
from tests.fakes import FakeDexAdapter


# =============================================================================
# Fixture helpers
# =============================================================================


def _position(
    token_id: int = 100,
    *,
    in_range: bool = True,
    current_price: Decimal = Decimal("0.000606"),
    price_lower: Decimal = Decimal("0.0005"),
    price_upper: Decimal = Decimal("0.0007"),
    tick_lower: int = -800,
    tick_upper: int = -400,
) -> PositionState:
    return PositionState(
        token_id=token_id,
        liquidity=1_000_000,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        tokens_owed_0=0,
        tokens_owed_1=0,
        price_lower=price_lower,
        price_upper=price_upper,
        current_price=current_price,
        in_range=in_range,
    )


class _FakeActionStore:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    async def insert_action(self, **kwargs: Any) -> int:
        self.actions.append(kwargs)
        return len(self.actions)

    async def get_actions(
        self, venue: str | None = None, action_type: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self.actions


class _FakeVenueConfigStore:
    def __init__(self) -> None:
        self._configs: dict[str, dict[str, Any]] = {}

    async def get_venue_config(self, venue: str) -> dict[str, Any] | None:
        return self._configs.get(venue)

    async def update_venue_config(self, venue: str, params: dict[str, Any]) -> None:
        self._configs[venue] = params


@pytest.fixture
async def price_db(tmp_path):
    """Real SQLite DB seeded with 15 price snapshots for the uni-base pool."""
    repo = DatabaseRepository(SQLiteConnectionManager(str(tmp_path / "lp_e2e.db")))
    await repo.connect()
    now = int(time.time() * 1000)
    for i in range(15):
        await repo.prices.insert_price_snapshot(
            PriceQuote(
                source="uni-base_pool",
                timestamp=now - (15 - i) * 60_000,
                bid=Decimal("0.000605"),
                ask=Decimal("0.000607"),
                mid=Decimal("0.000606"),
            )
        )
    yield repo.prices
    await repo.close()


def _make_rebalancer(action_store=None, venue_config_store=None):
    alerts: list[dict] = []
    rebalancer = LPRebalancer(
        broadcast=lambda e: alerts.append(e),
        price_store=None,  # overridden per test
        venue_config_store=venue_config_store or _FakeVenueConfigStore(),
        action_store=action_store or _FakeActionStore(),
    )
    return rebalancer, alerts


# =============================================================================
# Tests
# =============================================================================


class TestLPLifecycle:
    @pytest.mark.asyncio
    async def test_mint_from_idle_funds(self, price_db):
        """No position + funds above threshold → mint succeeds, one NFT created."""
        venue = FakeDexAdapter(
            token0_bal=Decimal("500000"),  # cNGN, well above lp_topup_threshold_cngn
            token1_bal=Decimal("600"),     # USDC, well above lp_topup_threshold_usdc
        )
        action_store = _FakeActionStore()
        rebalancer, alerts = _make_rebalancer(action_store=action_store)
        rebalancer._price_store = price_db

        result = await rebalancer.create_position(venue)

        assert result is True, "create_position must return True when mint succeeds"
        assert len(venue.get_owned_positions()) == 1, "exactly one NFT must be created"
        assert len(venue.minted) == 1, "exactly one mint call must be recorded"

        mint_actions = [a for a in action_store.actions if a["action_type"] == "mint_position"]
        assert len(mint_actions) == 1
        assert mint_actions[0]["status"] == "confirmed"
        assert mint_actions[0]["tx_hash"] == "0xabcdmint"

    @pytest.mark.asyncio
    async def test_rebalance_out_of_range(self, price_db):
        """Price exits range far enough → old position removed, new one minted."""
        old_pos = _position(
            token_id=55,
            in_range=False,
            current_price=Decimal("0.00080"),  # above range upper
            price_lower=Decimal("0.0005"),
            price_upper=Decimal("0.0007"),
        )
        venue = FakeDexAdapter(position=old_pos)
        action_store = _FakeActionStore()
        rebalancer, alerts = _make_rebalancer(action_store=action_store)
        rebalancer._price_store = price_db

        result = await rebalancer.rebalance(venue, token_id=55, position=old_pos)

        assert result is True
        # Old position burned, new one minted
        assert 55 not in venue.get_owned_positions(), "old position must be removed"
        assert len(venue.get_owned_positions()) == 1, "one new position must exist after rebalance"

        remove_actions = [a for a in action_store.actions if a["action_type"] == "remove_position"]
        mint_actions = [a for a in action_store.actions if a["action_type"] == "mint_position"]
        assert len(remove_actions) == 1 and remove_actions[0]["status"] == "confirmed"
        assert len(mint_actions) == 1 and mint_actions[0]["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_topup_idle_funds(self, price_db):
        """In-range position + idle funds above threshold → increase_liquidity called."""
        pos = _position(token_id=200, in_range=True)
        venue = FakeDexAdapter(
            position=pos,
            token0_bal=Decimal("500000"),
            token1_bal=Decimal("600"),
        )

        # Add increase_liquidity to FakeDexAdapter on the fly (it's not in fakes.py by default)
        topup_calls: list = []

        from engine.types import TxResult as _TxResult

        async def _increase_liquidity(token_id: int, amount0: int, amount1: int) -> _TxResult:
            topup_calls.append((token_id, amount0, amount1))
            return _TxResult(hash="0xtopup", status="confirmed")

        venue.increase_liquidity = _increase_liquidity  # type: ignore[method-assign]

        action_store = _FakeActionStore()
        rebalancer, alerts = _make_rebalancer(action_store=action_store)
        rebalancer._price_store = price_db

        await rebalancer.check_and_rebalance(venue)

        assert len(topup_calls) == 1, "increase_liquidity must be called once"
        topup_actions = [a for a in action_store.actions if a["action_type"] == "increase_liquidity"]
        assert len(topup_actions) == 1
        assert topup_actions[0]["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_withdraw_full_position(self, price_db):
        """withdraw_positions removes all NFTs and records confirmed removal for each."""
        pos1 = _position(token_id=10)
        pos2 = _position(token_id=11)
        venue = FakeDexAdapter()
        venue._positions = [pos1, pos2]

        action_store = _FakeActionStore()
        rebalancer, _ = _make_rebalancer(action_store=action_store)
        rebalancer._price_store = price_db

        results = await rebalancer.withdraw_positions(venue)

        assert len(results) == 2
        assert all(r["status"] == "confirmed" for r in results)
        assert venue.get_owned_positions() == [], "all positions must be removed"

        remove_actions = [a for a in action_store.actions if a["action_type"] == "manual_withdraw"]
        assert len(remove_actions) == 2
        assert all(a["status"] == "confirmed" for a in remove_actions)

    @pytest.mark.asyncio
    async def test_multi_position_halt(self, price_db):
        """Two LP NFTs → auto-management halts without touching either position."""
        pos1 = _position(token_id=30)
        pos2 = _position(token_id=31)
        venue = FakeDexAdapter()
        venue._positions = [pos1, pos2]

        action_store = _FakeActionStore()
        rebalancer, alerts = _make_rebalancer(action_store=action_store)
        rebalancer._price_store = price_db

        await rebalancer.check_and_rebalance(venue)

        # Both positions untouched
        assert set(venue.get_owned_positions()) == {30, 31}
        # A halt action and a warning alert must be emitted
        halt_actions = [a for a in action_store.actions if a["action_type"] == "lp_management_halted"]
        assert len(halt_actions) == 1
        warning_alerts = [a for a in alerts if a.get("severity") == "warning"]
        assert len(warning_alerts) == 1
        assert "halted" in warning_alerts[0]["message"].lower()

    @pytest.mark.asyncio
    async def test_multi_position_alert_not_repeated_for_same_incident(self, price_db):
        """Repeated checks with the same two NFTs must not duplicate the alert."""
        pos1 = _position(token_id=40)
        pos2 = _position(token_id=41)
        venue = FakeDexAdapter()
        venue._positions = [pos1, pos2]

        action_store = _FakeActionStore()
        rebalancer, alerts = _make_rebalancer(action_store=action_store)
        rebalancer._price_store = price_db

        await rebalancer.check_and_rebalance(venue)
        await rebalancer.check_and_rebalance(venue)

        # Alert fires only once per unique incident key (same {30,31} pair)
        warning_alerts = [a for a in alerts if a.get("severity") == "warning"]
        assert len(warning_alerts) == 1, "repeated calls must not re-broadcast the same halt alert"

    @pytest.mark.asyncio
    async def test_recovery_price_skew_updates_downside_skew(self, tmp_path):
        """Rebalancing with a recovery_price must update params.downside_skew in-place.

        When price exits range, the rebalancer calls _create_position_locked with
        recovery_price set to position.current_price. strategy.calculate_tick_range
        adjusts downside_skew based on how far current_price deviates from mean.
        The adjusted skew must be persisted via venue_config_store.

        Requires varying prices (std_dev > 0) so that deviation is non-zero.
        """
        repo = DatabaseRepository(SQLiteConnectionManager(str(tmp_path / "skew_test.db")))
        await repo.connect()
        now = int(time.time() * 1000)
        # Seed prices with variance: alternating 0.000560 / 0.000650 → mean ~0.000606, std_dev > 0
        alternating = [Decimal("0.000560"), Decimal("0.000650")]
        for i in range(15):
            mid = alternating[i % 2]
            await repo.prices.insert_price_snapshot(
                PriceQuote(
                    source="uni-base_pool",
                    timestamp=now - (15 - i) * 60_000,
                    bid=mid - Decimal("0.000005"),
                    ask=mid + Decimal("0.000005"),
                    mid=mid,
                )
            )
        price_store = repo.prices

        old_pos = _position(
            token_id=70,
            in_range=False,
            current_price=Decimal("0.00080"),  # well above mean; forces positive skew deviation
            price_lower=Decimal("0.0005"),
            price_upper=Decimal("0.0007"),
        )
        venue = FakeDexAdapter(position=old_pos)
        venue_config_store = _FakeVenueConfigStore()
        rebalancer, _ = _make_rebalancer(venue_config_store=venue_config_store)
        rebalancer._price_store = price_store

        original_skew = float(venue.params.downside_skew)
        result = await rebalancer.rebalance(venue, token_id=70, position=old_pos)
        await repo.close()

        assert result is True
        # venue_config_store must have been updated with the new (adjusted) params
        stored = venue_config_store._configs.get(venue.name)
        assert stored is not None, "venue config must be persisted after recovery_price rebalance"
        # The skew in the persisted config is stored as a JSON-serialisable value (str or float)
        stored_skew = float(stored["downside_skew"])
        assert stored_skew != original_skew, (
            "downside_skew must be adjusted when recovery_price deviates from mean"
        )

    @pytest.mark.asyncio
    async def test_mint_fails_returns_false_no_position_created(self, price_db):
        """When mint_position fails, create_position returns False and no NFT is recorded."""
        venue = FakeDexAdapter(mint_fails=True)
        action_store = _FakeActionStore()
        rebalancer, _ = _make_rebalancer(action_store=action_store)
        rebalancer._price_store = price_db

        result = await rebalancer.create_position(venue)

        assert result is False
        assert venue.get_owned_positions() == []
        mint_actions = [a for a in action_store.actions if a["action_type"] == "mint_position"]
        assert len(mint_actions) == 1
        assert mint_actions[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_rebalance_remove_fails_aborts_remint(self, price_db):
        """If remove_position fails, rebalance must not attempt a remint."""
        old_pos = _position(
            token_id=88,
            in_range=False,
            current_price=Decimal("0.00080"),
            price_lower=Decimal("0.0005"),
            price_upper=Decimal("0.0007"),
        )
        venue = FakeDexAdapter(position=old_pos, remove_fails=True)
        action_store = _FakeActionStore()
        rebalancer, alerts = _make_rebalancer(action_store=action_store)
        rebalancer._price_store = price_db

        result = await rebalancer.rebalance(venue, token_id=88, position=old_pos)

        assert result is False
        # Old position still in place — remove failed, nothing burned
        assert 88 in venue.get_owned_positions()
        # No mint action must have been attempted
        mint_actions = [a for a in action_store.actions if a["action_type"] == "mint_position"]
        assert mint_actions == [], "remint must not run when remove_position fails"
        # An error alert must be broadcast
        error_alerts = [a for a in alerts if a.get("severity") == "error"]
        assert len(error_alerts) == 1
