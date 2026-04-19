"""DB write correctness at each lifecycle stage.

These tests assert that the right fields are persisted at each state
transition — not that Pydantic validates fields, but that the DB layer
itself correctly preserves recovery-critical data across updates.

Non-obvious invariants:
- COALESCE in ON CONFLICT means a later update that passes None for
  buy_tx_hash or buy_amount_cngn must NOT overwrite an existing value.
- buy_amount_cngn=Decimal("0") is a valid filled amount (zero cNGN moved
  is not the same as "no buy recorded") and must survive the round-trip.
- CEX and DEX records share the arb_attempts table but are gated by pipeline.
  A DEX update targeting opp_id must not touch a CEX record with the same id.
"""

import time
from decimal import Decimal

import pytest

from engine.db.connection import SQLiteConnectionManager
from engine.db.repository import DatabaseRepository
from engine.types import ArbitrageOpportunity, DexArbOpportunity


@pytest.fixture
async def db(tmp_path):
    repo = DatabaseRepository(SQLiteConnectionManager(str(tmp_path / "test.db")))
    await repo.connect()
    yield repo
    await repo.close()


def _dex_opp(opp_id: str, status: str = "detected", **overrides) -> DexArbOpportunity:
    return DexArbOpportunity(
        id=opp_id,
        timestamp=int(time.time() * 1000),
        direction="UNI_BASE_TO_UNI_BSC_DELTA_BALANCE",
        optimal_size_usd=Decimal("500"),
        expected_profit_usd=Decimal("1.20"),
        cngn_transferred=Decimal("800000"),
        expected_usd_out=Decimal("501.20"),
        status=status,
        net_spread_bps=24,
        **overrides,
    )


def _cex_opp(opp_id: str, status: str = "detected") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        id=opp_id,
        timestamp=int(time.time() * 1000),
        buy_venue="quidax",
        sell_venue="uni-base",
        buy_price=Decimal("0.00061"),
        sell_price=Decimal("0.00064"),
        gross_spread_bps=50,
        net_spread_bps=30,
        recommended_size_usd=Decimal("300"),
        expected_profit_usd=Decimal("0.90"),
        status=status,
        direction="QUIDAX_TO_UNI_BASE",
    )


# =============================================================================
# DEX persistence
# =============================================================================


class TestDexPersistence:
    @pytest.mark.asyncio
    async def test_dex_insert_round_trips_all_required_fields(self, db):
        """Fields set at detection time must be readable back without loss."""
        opp = _dex_opp("dex-rt-1")
        await db.arbitrage.insert_dex_arbitrage_opportunity(opp)

        got = await db.arbitrage.get_dex_arbitrage_opportunity("dex-rt-1")
        assert got is not None
        assert got.direction == opp.direction
        assert got.optimal_size_usd == opp.optimal_size_usd
        assert got.expected_profit_usd == opp.expected_profit_usd
        assert got.cngn_transferred == opp.cngn_transferred
        assert got.status == "detected"

    @pytest.mark.asyncio
    async def test_buy_tx_hash_survives_status_only_update(self, db):
        """A later update that omits buy_tx_hash must not overwrite the stored value."""
        await db.arbitrage.insert_dex_arbitrage_opportunity(_dex_opp("dex-bth-1"))
        # First update: record half_open with buy_tx_hash
        await db.arbitrage.update_dex_arbitrage_execution_state(
            "dex-bth-1",
            status="half_open",
            buy_tx_hash="0xbuytx",
            buy_amount_cngn=Decimal("798000"),
        )
        # Second update: status change only — no buy_tx_hash supplied
        await db.arbitrage.update_dex_arbitrage_execution_state(
            "dex-bth-1",
            status="completed",
            actual_profit_usd=0.85,
        )

        got = await db.arbitrage.get_dex_arbitrage_opportunity("dex-bth-1")
        assert got.buy_tx_hash == "0xbuytx", (
            "buy_tx_hash must survive a later update that omits it"
        )
        assert got.status == "completed"

    @pytest.mark.asyncio
    async def test_buy_amount_cngn_zero_round_trips_as_zero_not_none(self, db):
        """buy_amount_cngn=Decimal('0') must come back as Decimal('0'), not None.

        Zero is a valid filled amount. Treating it as falsy (None) would prevent
        recovery from reading the stored amount.
        """
        await db.arbitrage.insert_dex_arbitrage_opportunity(_dex_opp("dex-zero-1"))
        await db.arbitrage.update_dex_arbitrage_execution_state(
            "dex-zero-1",
            status="half_open",
            buy_tx_hash="0xzerobuytx",
            buy_amount_cngn=Decimal("0"),
        )

        got = await db.arbitrage.get_dex_arbitrage_opportunity("dex-zero-1")
        assert got.buy_amount_cngn is not None, (
            "buy_amount_cngn=0 must not be stored/read as None"
        )
        assert got.buy_amount_cngn == Decimal("0")

    @pytest.mark.asyncio
    async def test_buy_amount_cngn_not_overwritten_by_later_update(self, db):
        """A subsequent update that does not supply buy_amount_cngn must leave it intact."""
        await db.arbitrage.insert_dex_arbitrage_opportunity(_dex_opp("dex-bac-1"))
        await db.arbitrage.update_dex_arbitrage_execution_state(
            "dex-bac-1",
            status="half_open",
            buy_tx_hash="0xbuytx",
            buy_amount_cngn=Decimal("798000"),
        )
        # Recovery completes — only passes status and profit, not buy_amount_cngn
        await db.arbitrage.update_dex_arbitrage_execution_state(
            "dex-bac-1",
            status="completed",
            sell_tx_hash="0xselltx",
            actual_profit_usd=0.82,
        )

        got = await db.arbitrage.get_dex_arbitrage_opportunity("dex-bac-1")
        assert got.buy_amount_cngn == Decimal("798000"), (
            "buy_amount_cngn must not be cleared by a later update that omits it"
        )
        assert got.sell_tx_hash == "0xselltx"
        assert got.buy_tx_hash == "0xbuytx"


# =============================================================================
# CEX persistence
# =============================================================================


class TestCexPersistence:
    @pytest.mark.asyncio
    async def test_cex_insert_round_trips_all_required_fields(self, db):
        opp = _cex_opp("cex-rt-1")
        await db.arbitrage.insert_arbitrage_opportunity(opp)

        got = await db.arbitrage.get_arbitrage_opportunity("cex-rt-1")
        assert got is not None
        assert got.buy_venue == "quidax"
        assert got.sell_venue == "uni-base"
        assert got.recommended_size_usd == Decimal("300")
        assert got.status == "detected"

    @pytest.mark.asyncio
    async def test_cex_buy_tx_hash_survives_status_update(self, db):
        """Same COALESCE invariant as DEX: buy_tx_hash must survive a status-only update."""
        await db.arbitrage.insert_arbitrage_opportunity(_cex_opp("cex-bth-1"))
        await db.arbitrage.update_arbitrage_opportunity(
            "cex-bth-1",
            status="half_open",
            buy_tx_hash="0xcexbuytx",
            buy_amount_cngn=300000.0,
        )
        await db.arbitrage.update_arbitrage_opportunity(
            "cex-bth-1",
            status="completed",
            actual_profit_usd=0.72,
        )

        got = await db.arbitrage.get_arbitrage_opportunity("cex-bth-1")
        assert got.buy_tx_hash == "0xcexbuytx"
        assert got.status == "completed"


# =============================================================================
# Pipeline isolation
# =============================================================================


class TestPipelineIsolation:
    @pytest.mark.asyncio
    async def test_dex_update_does_not_touch_cex_record_with_same_id(self, db):
        """CEX and DEX pipelines must be independent even when opp_id collides.

        The arb_attempts table uses a shared primary key (id) with a pipeline
        discriminator. update_dex_arbitrage_execution_state adds WHERE pipeline='dex_dex',
        so it must leave a CEX row with the same id completely untouched.
        """
        shared_id = "shared-opp-id"
        cex_opp = _cex_opp(shared_id, status="detected")
        await db.arbitrage.insert_arbitrage_opportunity(cex_opp)

        # This must be a no-op for the CEX record (different pipeline)
        await db.arbitrage.update_dex_arbitrage_execution_state(
            shared_id,
            status="half_open",
            buy_tx_hash="0xhijack",
        )

        cex_got = await db.arbitrage.get_arbitrage_opportunity(shared_id)
        assert cex_got is not None
        assert cex_got.status == "detected", (
            "DEX update must not modify a CEX record with the same opp_id"
        )
        assert cex_got.buy_tx_hash is None

    @pytest.mark.asyncio
    async def test_cex_update_does_not_touch_dex_record_with_same_id(self, db):
        """Symmetric isolation: CEX update must leave a DEX row with the same id alone."""
        shared_id = "shared-opp-id-2"
        dex_opp = _dex_opp(shared_id, status="detected")
        await db.arbitrage.insert_dex_arbitrage_opportunity(dex_opp)

        await db.arbitrage.update_arbitrage_opportunity(
            shared_id,
            status="completed",
            actual_profit_usd=1.0,
        )

        dex_got = await db.arbitrage.get_dex_arbitrage_opportunity(shared_id)
        assert dex_got is not None
        assert dex_got.status == "detected", (
            "CEX update must not modify a DEX record with the same opp_id"
        )
