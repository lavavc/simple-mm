"""Tests for price normalization and blended price computation."""

import pytest
from decimal import Decimal

from engine.api.schemas import PriceQuote
from engine.core.price_aggregation import (
    PriceNormalizer,
    BlendedPriceCalculator,
    NormalizedPrice,
    BlendedPrice,
    classify_venue,
    USDT_NGN_VENUES,
    CNGN_USD_VENUES,
    CNGN_NGN_VENUES,
)
from engine.core.venue_prices import VenuePrice


# =============================================================================
# Helpers
# =============================================================================


def _make_venue_price(
    venue: str,
    pair: str,
    bid: Decimal,
    ask: Decimal,
    mid: Decimal,
    source: str | None = None,
) -> VenuePrice:
    """Create a VenuePrice with a valid quote."""
    quote = PriceQuote(
        source=source or venue,
        timestamp=1700000000000,
        bid=bid,
        ask=ask,
        mid=mid,
    )
    return VenuePrice(venue=venue, pair=pair, quote=quote)


def _make_venue_prices() -> dict[str, VenuePrice]:
    """Standard set of venue prices for testing."""
    return {
        "bybit": _make_venue_price(
            "bybit", "USDT/NGN",
            bid=Decimal("1435"), ask=Decimal("1437"), mid=Decimal("1436"),
            source="bybit_p2p",
        ),
        "quidax": _make_venue_price(
            "quidax", "cNGN/USDT",
            bid=Decimal("0.000696"), ask=Decimal("0.000698"), mid=Decimal("0.000697"),
            source="quidax",
        ),
        "aerodrome": _make_venue_price(
            "aerodrome", "cNGN/USDC",
            bid=Decimal("0.000695"), ask=Decimal("0.000697"), mid=Decimal("0.000696"),
            source="aerodrome_pool",
        ),
        "pancakeswap": _make_venue_price(
            "pancakeswap", "cNGN/USDT",
            bid=Decimal("0.000700"), ask=Decimal("0.000702"), mid=Decimal("0.000701"),
            source="pancakeswap_pool",
        ),
    }


# =============================================================================
# Venue classification
# =============================================================================


class TestVenueClassification:
    """Test venue classification."""

    def test_bybit_classified_as_usdt_ngn(self):
        assert classify_venue("bybit") == "USDT/NGN"

    def test_quidax_classified_as_cngn_usdc(self):
        assert classify_venue("quidax") == "cNGN/USDC"

    def test_aerodrome_classified_as_cngn_usdc(self):
        assert classify_venue("aerodrome") == "cNGN/USDC"

    def test_blockradar_classified_as_cngn_ngn(self):
        assert classify_venue("blockradar") == "cNGN/NGN"

    def test_unknown_venue(self):
        assert classify_venue("unknown_exchange") == "unknown"


# =============================================================================
# PriceNormalizer
# =============================================================================


class TestPriceNormalizer:
    """Test price normalization to cNGN/USD."""

    def setup_method(self):
        self.normalizer = PriceNormalizer()

    def test_normalize_bybit_usdt_ngn(self):
        """Bybit USDT/NGN should be inverted: cNGN/USD = 1/mid."""
        prices = {
            "bybit": _make_venue_price(
                "bybit", "USDT/NGN",
                bid=Decimal("1436"), ask=Decimal("1438"), mid=Decimal("1437"),
                source="bybit_p2p",
            ),
        }
        result = self.normalizer.normalize(prices)

        assert "bybit" in result
        # 1/1437 ≈ 0.000696
        expected = Decimal("1") / Decimal("1437")
        assert abs(result["bybit"].cngn_usd - expected) < Decimal("0.000001")
        assert result["bybit"].basis == "USDT/NGN"

    def test_normalize_quidax_cngn_usdt(self):
        """Quidax cNGN/USDT is already in cNGN/USD form."""
        prices = {
            "quidax": _make_venue_price(
                "quidax", "cNGN/USDT",
                bid=Decimal("0.000696"), ask=Decimal("0.000698"), mid=Decimal("0.000697"),
            ),
        }
        result = self.normalizer.normalize(prices)

        assert "quidax" in result
        assert result["quidax"].cngn_usd == Decimal("0.000697")

    def test_normalize_aerodrome_cngn_usdc(self):
        """Aerodrome cNGN/USDC is already cNGN/USD."""
        prices = {
            "aerodrome": _make_venue_price(
                "aerodrome", "cNGN/USDC",
                bid=Decimal("0.000695"), ask=Decimal("0.000697"), mid=Decimal("0.000696"),
                source="aerodrome_pool",
            ),
        }
        result = self.normalizer.normalize(prices)

        assert "aerodrome" in result
        assert result["aerodrome"].cngn_usd == Decimal("0.000696")

    def test_normalize_blockradar_with_cross_rate(self):
        """Blockradar cNGN/NGN needs USDT/NGN cross-rate."""
        prices = {
            "bybit": _make_venue_price(
                "bybit", "USDT/NGN",
                bid=Decimal("1436"), ask=Decimal("1438"), mid=Decimal("1437"),
                source="bybit_p2p",
            ),
            "blockradar": _make_venue_price(
                "blockradar", "cNGN/NGN",
                bid=Decimal("0.998"), ask=Decimal("1.002"), mid=Decimal("1.0"),
                source="blockradar",
            ),
        }
        result = self.normalizer.normalize(prices)

        assert "blockradar" in result
        # cNGN/USD = blockradar_mid / usdt_ngn_mid = 1.0 / 1437 ≈ 0.000696
        expected = Decimal("1.0") / Decimal("1437")
        assert abs(result["blockradar"].cngn_usd - expected) < Decimal("0.000001")

    def test_normalize_blockradar_without_cross_rate(self):
        """Blockradar should be excluded if no USDT/NGN cross-rate available."""
        prices = {
            "blockradar": _make_venue_price(
                "blockradar", "cNGN/NGN",
                bid=Decimal("0.998"), ask=Decimal("1.002"), mid=Decimal("1.0"),
            ),
        }
        result = self.normalizer.normalize(prices)
        assert "blockradar" not in result

    def test_skip_invalid_venue_prices(self):
        """Venues with no quote or zero mid should be skipped."""
        zero_quote = PriceQuote(
            source="test", timestamp=1700000000000,
            bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0"),
        )
        prices = {
            "quidax": VenuePrice(venue="quidax", pair="cNGN/USDT", quote=zero_quote),
        }
        result = self.normalizer.normalize(prices)
        assert "quidax" not in result

    def test_skip_venue_with_no_quote(self):
        """Venues with None quote should be skipped."""
        prices = {
            "quidax": VenuePrice(venue="quidax", pair="cNGN/USDT", quote=None, error="timeout"),
        }
        result = self.normalizer.normalize(prices)
        assert len(result) == 0

    def test_normalize_multiple_venues(self):
        """All valid venues should be normalized."""
        prices = _make_venue_prices()
        result = self.normalizer.normalize(prices)

        assert len(result) == 4
        assert all(v in result for v in ["bybit", "quidax", "aerodrome", "pancakeswap"])

        # All should be in a similar range (~0.0007)
        for np in result.values():
            assert Decimal("0.0005") < np.cngn_usd < Decimal("0.001")


# =============================================================================
# BlendedPriceCalculator — VWAP (sync portion)
# =============================================================================


class TestVWAP:
    """Test cross-venue VWAP computation."""

    def test_vwap_equal_weights(self):
        """VWAP with equal weights should be arithmetic mean."""
        normalizer = PriceNormalizer()
        prices = _make_venue_prices()
        normalized = normalizer.normalize(prices)

        # Use a dummy aggregator — we just need compute_vwap which is sync
        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}
        vwap = calc.compute_vwap(normalized)

        # Mean of ~0.000696, 0.000697, 0.000696, 0.000701
        values = [np.cngn_usd for np in normalized.values()]
        expected = sum(values) / len(values)
        assert abs(vwap - expected) < Decimal("0.000001")

    def test_vwap_custom_weights(self):
        """VWAP with custom weights should bias toward heavier venues."""
        normalizer = PriceNormalizer()
        prices = _make_venue_prices()
        normalized = normalizer.normalize(prices)

        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}

        weights = {
            "aerodrome": Decimal("10"),
            "quidax": Decimal("1"),
            "bybit": Decimal("1"),
            "pancakeswap": Decimal("1"),
        }
        vwap = calc.compute_vwap(normalized, weights)

        # Should be closer to aerodrome's price than the simple mean
        aerodrome_price = normalized["aerodrome"].cngn_usd
        assert abs(vwap - aerodrome_price) < Decimal("0.00001")

    def test_vwap_empty_input(self):
        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}
        assert calc.compute_vwap({}) == Decimal("0")

    def test_vwap_single_venue(self):
        normalizer = PriceNormalizer()
        prices = {
            "quidax": _make_venue_price(
                "quidax", "cNGN/USDT",
                bid=Decimal("0.000696"), ask=Decimal("0.000698"), mid=Decimal("0.000697"),
            ),
        }
        normalized = normalizer.normalize(prices)

        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}
        vwap = calc.compute_vwap(normalized)
        assert vwap == Decimal("0.000697")


# =============================================================================
# Source-to-venue mapping
# =============================================================================


class TestSourceToVenue:
    """Test the _source_to_venue static method."""

    def test_bybit_p2p(self):
        assert BlendedPriceCalculator._source_to_venue("bybit_p2p") == "bybit"

    def test_quidax(self):
        assert BlendedPriceCalculator._source_to_venue("quidax") == "quidax"

    def test_aerodrome_pool(self):
        assert BlendedPriceCalculator._source_to_venue("aerodrome_pool") == "aerodrome"

    def test_pancakeswap_pool(self):
        assert BlendedPriceCalculator._source_to_venue("pancakeswap_pool") == "pancakeswap"

    def test_blockradar(self):
        assert BlendedPriceCalculator._source_to_venue("blockradar") == "blockradar"

    def test_unknown_passthrough(self):
        assert BlendedPriceCalculator._source_to_venue("something_new") == "something_new"


# =============================================================================
# Single-price normalization
# =============================================================================


class TestNormalizeSinglePrice:
    """Test _normalize_single_price helper."""

    def test_bybit_usdt_ngn_inverted(self):
        result = BlendedPriceCalculator._normalize_single_price("bybit", Decimal("1437"))
        assert result is not None
        assert abs(result - Decimal("1") / Decimal("1437")) < Decimal("0.000001")

    def test_quidax_passthrough(self):
        result = BlendedPriceCalculator._normalize_single_price("quidax", Decimal("0.000697"))
        assert result == Decimal("0.000697")

    def test_aerodrome_passthrough(self):
        result = BlendedPriceCalculator._normalize_single_price("aerodrome", Decimal("0.000696"))
        assert result == Decimal("0.000696")

    def test_blockradar_returns_none(self):
        """Blockradar can't be normalized without cross-rate."""
        result = BlendedPriceCalculator._normalize_single_price("blockradar", Decimal("1.0"))
        assert result is None

    def test_zero_price_returns_none(self):
        result = BlendedPriceCalculator._normalize_single_price("quidax", Decimal("0"))
        assert result is None

    def test_negative_price_returns_none(self):
        result = BlendedPriceCalculator._normalize_single_price("quidax", Decimal("-1"))
        assert result is None


# =============================================================================
# Confidence score
# =============================================================================


class TestConfidence:
    """Test confidence score computation."""

    def test_perfect_agreement(self):
        """All venues at same price → 100% confidence."""
        normalized = {
            "a": NormalizedPrice(venue="a", cngn_usd=Decimal("0.000700"), raw_quote=PriceQuote(source="a", timestamp=0, bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0")), basis="cNGN/USDC", timestamp=0),
            "b": NormalizedPrice(venue="b", cngn_usd=Decimal("0.000700"), raw_quote=PriceQuote(source="b", timestamp=0, bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0")), basis="cNGN/USDT", timestamp=0),
        }
        confidence = BlendedPriceCalculator._compute_confidence(normalized, Decimal("0.000700"))
        assert confidence == 1.0

    def test_one_outlier(self):
        """One venue far from VWAP → partial confidence."""
        normalized = {
            "a": NormalizedPrice(venue="a", cngn_usd=Decimal("0.000700"), raw_quote=PriceQuote(source="a", timestamp=0, bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0")), basis="cNGN/USDC", timestamp=0),
            "b": NormalizedPrice(venue="b", cngn_usd=Decimal("0.000700"), raw_quote=PriceQuote(source="b", timestamp=0, bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0")), basis="cNGN/USDT", timestamp=0),
            "c": NormalizedPrice(venue="c", cngn_usd=Decimal("0.000800"), raw_quote=PriceQuote(source="c", timestamp=0, bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0")), basis="cNGN/USDT", timestamp=0),
        }
        confidence = BlendedPriceCalculator._compute_confidence(normalized, Decimal("0.000700"))
        # a and b agree (within 1%), c is 14% off
        assert confidence == pytest.approx(2 / 3, abs=0.01)

    def test_empty_returns_zero(self):
        assert BlendedPriceCalculator._compute_confidence({}, Decimal("0.0007")) == 0.0

    def test_zero_vwap_returns_zero(self):
        normalized = {
            "a": NormalizedPrice(venue="a", cngn_usd=Decimal("0.000700"), raw_quote=PriceQuote(source="a", timestamp=0, bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0")), basis="cNGN/USDC", timestamp=0),
        }
        assert BlendedPriceCalculator._compute_confidence(normalized, Decimal("0")) == 0.0


# =============================================================================
# BlendedPrice dataclass
# =============================================================================


class TestBlendedPrice:
    """Test BlendedPrice dataclass properties."""

    def test_reference_price_ngn(self):
        """reference_price_ngn should be 1/vwap."""
        bp = BlendedPrice(
            vwap=Decimal("0.000700"),
            twap_5m=Decimal("0.000700"),
            twap_1h=Decimal("0.000700"),
            venue_prices={},
            timestamp=0,
            num_sources=3,
            confidence=1.0,
        )
        expected = Decimal("1") / Decimal("0.000700")
        assert abs(bp.reference_price_ngn - expected) < Decimal("0.01")

    def test_reference_price_ngn_zero_vwap(self):
        bp = BlendedPrice(
            vwap=Decimal("0"),
            twap_5m=Decimal("0"),
            twap_1h=Decimal("0"),
            venue_prices={},
            timestamp=0,
            num_sources=0,
            confidence=0.0,
        )
        assert bp.reference_price_ngn == Decimal("0")
