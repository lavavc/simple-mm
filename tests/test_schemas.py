"""Tests for API schema validation and serialization."""

import pytest
from decimal import Decimal
from pydantic import ValidationError

from engine.api.schemas import (
    PriceQuote,
    Position,
    VenuePriceResponse,
    VenueStatus,
    SystemStatus,
    DexParams,
    CexParams,
    WalletParams,
    GlobalPosition,
    Alert,
    ArbitrageParams,
    ArbitrageOpportunity,
    ArbitrageStatus,
    BlendedPriceResponse,
    NormalizedPriceResponse,
)


# =============================================================================
# PriceQuote
# =============================================================================


class TestPriceQuote:
    """Test PriceQuote schema."""

    def test_create_basic(self):
        q = PriceQuote(
            source="quidax",
            timestamp=1700000000000,
            bid=Decimal("0.000696"),
            ask=Decimal("0.000698"),
            mid=Decimal("0.000697"),
        )
        assert q.source == "quidax"
        assert q.mid == Decimal("0.000697")

    def test_from_string_decimals(self):
        """Should accept string values for bid/ask/mid."""
        q = PriceQuote(
            source="test",
            timestamp=0,
            bid="0.000696",
            ask="0.000698",
            mid="0.000697",
        )
        assert q.mid == Decimal("0.000697")

    def test_json_roundtrip(self):
        q = PriceQuote(
            source="test", timestamp=1000,
            bid=Decimal("1.5"), ask=Decimal("2.5"), mid=Decimal("2.0"),
        )
        json_str = q.model_dump_json()
        q2 = PriceQuote.model_validate_json(json_str)
        assert q2.mid == q.mid


# =============================================================================
# VenuePriceResponse
# =============================================================================


class TestVenuePriceResponse:

    def test_with_quote(self):
        r = VenuePriceResponse(
            venue="quidax",
            pair="cNGN/USDT",
            quote=PriceQuote(
                source="quidax", timestamp=0,
                bid=Decimal("0.0007"), ask=Decimal("0.0007"), mid=Decimal("0.0007"),
            ),
            age_seconds=5.0,
        )
        assert r.quote is not None
        assert r.error is None

    def test_with_error(self):
        r = VenuePriceResponse(
            venue="blockradar",
            pair="cNGN/NGN",
            error="No price returned",
            age_seconds=30.0,
        )
        assert r.quote is None
        assert r.error == "No price returned"


# =============================================================================
# ArbitrageParams
# =============================================================================


class TestArbitrageParams:

    def test_defaults(self):
        p = ArbitrageParams()
        assert p.min_spread_bps == 150
        assert p.min_net_profit_bps == 50
        assert p.dex_swap_fee_bps == 30
        assert p.max_single_trade_usd == Decimal("100")

    def test_custom_values(self):
        p = ArbitrageParams(
            min_spread_bps=200,
            max_single_trade_usd=Decimal("5000"),
        )
        assert p.min_spread_bps == 200
        assert p.max_single_trade_usd == Decimal("5000")


# =============================================================================
# ArbitrageOpportunity
# =============================================================================


class TestArbitrageOpportunity:

    def test_create(self):
        o = ArbitrageOpportunity(
            id="test-123",
            timestamp=1700000000000,
            buy_venue="aerodrome",
            sell_venue="quidax",
            buy_price=Decimal("0.000690"),
            sell_price=Decimal("0.000750"),
            gross_spread_bps=870,
            net_spread_bps=800,
            recommended_size_usd=Decimal("1000"),
            expected_profit_usd=Decimal("80"),
            status="detected",
        )
        assert o.buy_venue == "aerodrome"
        assert o.status == "detected"

    def test_status_values(self):
        """All valid status values should work."""
        for status in ["detected", "executing", "completed", "abandoned", "expired"]:
            o = ArbitrageOpportunity(
                id="t", timestamp=0, buy_venue="a", sell_venue="b",
                buy_price=Decimal("1"), sell_price=Decimal("2"),
                gross_spread_bps=100, net_spread_bps=50,
                recommended_size_usd=Decimal("100"),
                expected_profit_usd=Decimal("5"),
                status=status,
            )
            assert o.status == status


# =============================================================================
# GlobalPosition
# =============================================================================


class TestGlobalPosition:

    def test_create(self):
        gp = GlobalPosition(
            total_cngn=Decimal("100000"),
            total_usdt=Decimal("50"),
            total_usdc=Decimal("30"),
            total_usd_value=Decimal("150"),
            delta_ratio=Decimal("0.47"),
            target_delta=Decimal("0.50"),
        )
        assert gp.total_cngn == Decimal("100000")
        assert gp.delta_ratio == Decimal("0.47")


# =============================================================================
# BlendedPriceResponse / NormalizedPriceResponse
# =============================================================================


class TestBlendedPriceResponse:

    def test_create(self):
        r = BlendedPriceResponse(
            vwap=Decimal("0.000697"),
            twap_5m=Decimal("0.000696"),
            twap_1h=Decimal("0.000695"),
            reference_price_ngn=Decimal("1436"),
            venue_prices={"quidax": Decimal("0.000697")},
            timestamp=1700000000000,
            num_sources=3,
            confidence=0.85,
        )
        assert r.vwap == Decimal("0.000697")
        assert r.confidence == 0.85


class TestNormalizedPriceResponse:

    def test_create(self):
        r = NormalizedPriceResponse(
            venue="quidax",
            cngn_usd=Decimal("0.000697"),
            basis="cNGN/USDT",
            raw_mid=Decimal("0.000697"),
            timestamp=1700000000000,
        )
        assert r.venue == "quidax"


# =============================================================================
# Alert
# =============================================================================


class TestAlert:

    def test_create(self):
        a = Alert(
            id=1,
            timestamp=1700000000000,
            severity="warning",
            category="refill",
            message="Low balance",
        )
        assert a.severity == "warning"
        assert a.acknowledged is False

    def test_severity_values(self):
        for sev in ["info", "warning", "critical"]:
            a = Alert(
                id=1, timestamp=0, severity=sev,
                category="test", message="test",
            )
            assert a.severity == sev
